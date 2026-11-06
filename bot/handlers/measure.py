"""Хендлеры записи давления (SPEC.md, §6.3).

Три шага кнопками (систолическое → диастолическое → пульс); «Другое» —
свободный ввод (FSM) с «Отмена». После сохранения — «✅ … Записано» (+ вежливая
строка при ≥180/110) и кнопки опциональных шагов: заметка, «Приняли
лекарство?», самочувствие. «Да» планирует повторный замер
(`RemindersService.ensure_remeasure`) с кнопкой «Не надо»; после первой «Да»
без настроенных лекарств — мягкая подсказка «Напоминать, какие лекарства?»
(один раз). Кнопка «✏️ Изменить» (`m:edit`) правит последнюю запись: те же
шаги кнопок, финальное сохранение — UPDATE по `M_EDIT_ID` (время записи не
меняется). Кнопки редактируют текущее сообщение; ответы на свободный ввод —
новыми сообщениями. Черновик живёт в FSM (`MeasureInput`), id сохранённой
записи — в data (м. M_ID), id правки — в M_EDIT_ID: чужие id через
callback_data сюда не попадают.
"""
from __future__ import annotations

from typing import Any, Optional

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot import keyboards, schemas, texts
from bot.models import User
from bot.services import measurements as measurements_service
from bot.services import medications as medications_service
from bot.services.reminders import RemindersService

router = Router(name="measure")

#: Порог «высокого» давления для вежливой строки после сохранения (§6.3).
HIGH_SYSTOLIC = 180
HIGH_DIASTOLIC = 110

#: Ключи черновика измерения в FSM.
M_SYS = "m_sys"
M_DIA = "m_dia"
M_PULSE = "m_pulse"
M_ID = "m_id"
#: Id редактируемой записи («последняя» фиксируется в момент входа в правку, §6.3).
M_EDIT_ID = "m_edit_id"


class MeasureInput(StatesGroup):
    """FSM-состояния свободного ввода флоу записи (§15)."""

    sys_other = State()
    dia_other = State()
    pulse_other = State()
    note_input = State()
    feeling_other = State()


# ─────────────────────────── Экраны ───────────────────────────


async def _send(
    message: Message, text: str, markup: InlineKeyboardMarkup, *, via_message: bool
) -> None:
    """Отправить экран: свободный ввод — новым сообщением, кнопки — редактированием."""
    if via_message:
        await message.answer(text, reply_markup=markup)
    else:
        await message.edit_text(text, reply_markup=markup)  # type: ignore[union-attr]


async def _hub(message: Message, data: dict[str, Any], *, via_message: bool = False) -> None:
    """Вернуть «✅ Записано/Обновлено» с кнопками опциональных шагов (повторное открытие)."""
    if data.get(M_EDIT_ID) is not None:
        text = texts.fmt_updated(data[M_SYS], data[M_DIA], data.get(M_PULSE))
    else:
        text = texts.fmt_saved(data[M_SYS], data[M_DIA], data.get(M_PULSE))
    await _send(message, text, keyboards.saved_kb(), via_message=via_message)


async def _to_menu(callback: CallbackQuery) -> None:
    """Устаревшие кнопки без черновика — в главное меню (сообщение редактируется)."""
    await callback.message.edit_text(  # type: ignore[union-attr]
        texts.MAIN_MENU, reply_markup=keyboards.main_menu()
    )
    await callback.answer()


async def _measurement_id(callback: CallbackQuery, state: FSMContext) -> Optional[int]:
    """Id записи черновика; без него — главное меню (устаревшие кнопки)."""
    data = await state.get_data()
    measurement_id = data.get(M_ID)
    if measurement_id is None:
        await _to_menu(callback)
        return None
    return measurement_id


# ─────────────────────────── Начало и отмена ───────────────────────────


@router.callback_query(F.data.in_({"menu:measure", "rem:write", "rem:now"}))
async def cb_measure_begin(callback: CallbackQuery, state: FSMContext) -> None:
    """«Записать давление» / «Записать сейчас» — шаг 1 (§6.3, §6.7).

    Прошлый черновик сбрасывается.
    """
    await state.clear()
    await callback.message.edit_text(  # type: ignore[union-attr]
        texts.ASK_SYSTOLIC, reply_markup=keyboards.systolic_kb()
    )
    await callback.answer()


@router.callback_query(F.data == "m:cancel")
async def cb_measure_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    """«Отмена» — сброс черновика и FSM, возврат в главное меню."""
    await state.clear()
    await callback.message.edit_text(  # type: ignore[union-attr]
        texts.MAIN_MENU, reply_markup=keyboards.main_menu()
    )
    await callback.answer()


@router.callback_query(F.data == "m:edit")
async def cb_edit_begin(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    """«✏️ Изменить» — правка последней записи (§6.3, §8).

    «Последняя» резолвится на сервере (id в callback_data не передаётся).
    Прошлый черновик сбрасывается; цель правки фиксируется (M_EDIT_ID).
    """
    tg_id = callback.from_user.id
    user = await measurements_service.get_or_create_user(session, tg_id)
    measurement = await measurements_service.get_latest_measurement(session, user)
    if measurement is None:
        await callback.answer(texts.NOT_YOUR_RECORD)
        return
    await state.clear()
    await state.update_data(**{M_EDIT_ID: measurement.id, M_ID: measurement.id})
    await callback.message.edit_text(  # type: ignore[union-attr]
        texts.ASK_SYSTOLIC, reply_markup=keyboards.systolic_kb()
    )
    await callback.answer()


# ─────────────────────────── Шаг 1: систолическое ───────────────────────────


@router.callback_query(F.data.startswith("m:sys:"))
async def cb_systolic(callback: CallbackQuery, state: FSMContext) -> None:
    value = callback.data.split(":", 2)[2]  # type: ignore[union-attr]
    if value == "other":
        await state.set_state(MeasureInput.sys_other)
        text, markup = texts.PROMPT_SYSTOLIC_OTHER, keyboards.input_cancel_kb()
    else:
        try:
            sys_v = schemas.validate_int(value, 70, 260, "систолическое")
        except ValueError as exc:
            await callback.answer(str(exc))
            return
        await state.update_data(**{M_SYS: sys_v})
        text, markup = texts.ASK_DIASTOLIC, keyboards.diastolic_kb()
    await callback.message.edit_text(text, reply_markup=markup)  # type: ignore[union-attr]
    await callback.answer()


@router.message(MeasureInput.sys_other)
async def msg_systolic(message: Message, state: FSMContext) -> None:
    try:
        sys_v = schemas.validate_int(message.text or "", 70, 260, "систолическое")
    except ValueError as exc:
        await message.answer(str(exc), reply_markup=keyboards.input_cancel_kb())
        return
    await state.update_data(**{M_SYS: sys_v})
    await state.set_state(None)
    await message.answer(texts.ASK_DIASTOLIC, reply_markup=keyboards.diastolic_kb())


# ─────────────────────────── Шаг 2: диастолическое ───────────────────────────


@router.callback_query(F.data.startswith("m:dia:"))
async def cb_diastolic(callback: CallbackQuery, state: FSMContext) -> None:
    value = callback.data.split(":", 2)[2]  # type: ignore[union-attr]
    if value == "other":
        await state.set_state(MeasureInput.dia_other)
        text, markup = texts.PROMPT_DIASTOLIC_OTHER, keyboards.input_cancel_kb()
    else:
        try:
            dia_v = schemas.validate_int(value, 40, 160, "диастолическое")
        except ValueError as exc:
            await callback.answer(str(exc))
            return
        await state.update_data(**{M_DIA: dia_v})
        text, markup = texts.ASK_PULSE, keyboards.pulse_kb()
    await callback.message.edit_text(text, reply_markup=markup)  # type: ignore[union-attr]
    await callback.answer()


@router.message(MeasureInput.dia_other)
async def msg_diastolic(message: Message, state: FSMContext) -> None:
    try:
        dia_v = schemas.validate_int(message.text or "", 40, 160, "диастолическое")
    except ValueError as exc:
        await message.answer(str(exc), reply_markup=keyboards.input_cancel_kb())
        return
    await state.update_data(**{M_DIA: dia_v})
    await state.set_state(None)
    await message.answer(texts.ASK_PULSE, reply_markup=keyboards.pulse_kb())


# ─────────────────────────── Шаг 3: пульс и сохранение ───────────────────────────


@router.callback_query(F.data.startswith("m:pulse:"))
async def cb_pulse(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    value = callback.data.split(":", 2)[2]  # type: ignore[union-attr]
    data = await state.get_data()
    if M_SYS not in data or M_DIA not in data:
        await _to_menu(callback)  # устаревшие кнопки без черновика
        return
    if value == "other":
        await state.set_state(MeasureInput.pulse_other)
        await callback.message.edit_text(  # type: ignore[union-attr]
            texts.PROMPT_PULSE_OTHER, reply_markup=keyboards.input_cancel_kb()
        )
        await callback.answer()
        return
    pulse = None
    if value != "skip":
        try:
            pulse = schemas.validate_int(value, 30, 220, "пульс")
        except ValueError as exc:
            await callback.answer(str(exc))
            return
    await state.update_data(**{M_PULSE: pulse})
    await _save(callback.message, state, session, callback.from_user.id)  # type: ignore[arg-type]
    await callback.answer()


@router.message(MeasureInput.pulse_other)
async def msg_pulse(message: Message, state: FSMContext, session: AsyncSession) -> None:
    try:
        pulse = schemas.validate_int(message.text or "", 30, 220, "пульс")
    except ValueError as exc:
        await message.answer(str(exc), reply_markup=keyboards.input_cancel_kb())
        return
    await state.update_data(**{M_PULSE: pulse})
    await state.set_state(None)
    tg_id = message.from_user.id  # type: ignore[union-attr]
    await _save(message, state, session, tg_id, via_message=True)


async def _save(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    telegram_id: int,
    *,
    via_message: bool = False,
) -> None:
    """Сохранить черновик: INSERT новой записи или UPDATE правки (§6.3).

    Сначала commit записи, FSM обновляется после (§7.2: ничего человеческого
    внутри транзакции); дубль за минуту — «Это давление уже записано» (только
    для новой записи); правка — UPDATE без дедупа и без смены времени записи.
    """
    data = await state.get_data()
    if M_SYS not in data or M_DIA not in data:
        await _send(  # устаревшие кнопки без черновика — в главное меню
            message, texts.MAIN_MENU, keyboards.main_menu(), via_message=via_message
        )
        return
    sys_v, dia_v, pulse = data[M_SYS], data[M_DIA], data.get(M_PULSE)
    user = await measurements_service.get_or_create_user(session, telegram_id)
    edit_id = data.get(M_EDIT_ID)
    if edit_id is None:
        try:
            measurement = await measurements_service.save_measurement(
                session, user, sys_v, dia_v, pulse
            )
        except measurements_service.DuplicateMeasurementError:
            await state.clear()
            await _send(
                message,
                texts.DUPLICATE_MEASUREMENT,
                keyboards.back_to_menu(),
                via_message=via_message,
            )
            return
        text = texts.fmt_saved(sys_v, dia_v, pulse)
    else:
        measurement = await measurements_service.update_measurement_values(
            session, user, edit_id, sys_v, dia_v, pulse
        )
        if measurement is None:  # запись исчезла между входом в правку и сохранением
            await state.clear()
            await _send(
                message, texts.NOT_YOUR_RECORD, keyboards.back_to_menu(), via_message=via_message
            )
            return
        text = texts.fmt_updated(sys_v, dia_v, pulse)
    await session.commit()
    await state.update_data(**{M_ID: measurement.id})
    if sys_v >= HIGH_SYSTOLIC or dia_v >= HIGH_DIASTOLIC:
        text = f"{text}\n{texts.HIGH_PRESSURE_WARNING}"
    await _send(message, text, keyboards.saved_kb(), via_message=via_message)


# ─────────────────────────── Заметка ───────────────────────────


@router.callback_query(F.data == "m:note")
async def cb_note(callback: CallbackQuery, state: FSMContext) -> None:
    """«Добавить заметку» — свободный ввод (FSM)."""
    if await _measurement_id(callback, state) is None:
        return
    await state.set_state(MeasureInput.note_input)
    await callback.message.edit_text(  # type: ignore[union-attr]
        texts.PROMPT_NOTE, reply_markup=keyboards.note_cancel_kb()
    )
    await callback.answer()


@router.message(MeasureInput.note_input)
async def msg_note(message: Message, state: FSMContext, session: AsyncSession) -> None:
    try:
        note = schemas.validate_note(message.text or "")
    except ValueError as exc:
        await message.answer(str(exc), reply_markup=keyboards.note_cancel_kb())
        return
    data = await state.get_data()
    tg_id = message.from_user.id  # type: ignore[union-attr]
    user = await measurements_service.get_or_create_user(session, tg_id)
    await measurements_service.add_note(session, user, data[M_ID], note)
    await session.commit()
    await state.set_state(None)
    await message.answer(texts.NOTE_SAVED, reply_markup=keyboards.saved_kb())


# ─────────────────────────── «Приняли лекарство?» ───────────────────────────


@router.callback_query(F.data == "m:med")
async def cb_med(callback: CallbackQuery, state: FSMContext) -> None:
    if await _measurement_id(callback, state) is None:
        return
    await callback.message.edit_text(  # type: ignore[union-attr]
        texts.MED_ASK, reply_markup=keyboards.medication_taken_kb()
    )
    await callback.answer()


@router.callback_query(F.data == "m:med:yes")
async def cb_med_yes(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    reminders: RemindersService,
) -> None:
    """«Да»: флаг TRUE + повтор замера через `med_remeasure_delay` (§6.3).

    Повтор идемпотентен по id записи; кнопка «Не надо» — отмена. После первой
    «Да» без настроенных лекарств — мягкая подсказка добавить их (один раз).
    """
    if await _measurement_id(callback, state) is None:
        return
    data = await state.get_data()
    measurement_id = data[M_ID]
    tg_id = callback.from_user.id
    user = await measurements_service.get_or_create_user(session, tg_id)
    await measurements_service.set_medication_taken(session, user, measurement_id, True)
    await session.commit()
    delay = user.med_remeasure_delay
    if delay:
        await reminders.ensure_remeasure(user.id, measurement_id, delay)
        text = texts.MED_REMEASURE_PLANNED.format(delay=delay)
        markup = keyboards.no_remeasure_kb(measurement_id)
    else:
        text = texts.MED_REMEASURE_DISABLED
        markup = keyboards.saved_kb()
    await callback.message.edit_text(text, reply_markup=markup)  # type: ignore[union-attr]
    await callback.answer()
    if await _should_ask_meds(session, user):
        await callback.message.answer(  # type: ignore[union-attr]
            texts.MEDS_ASK_PROMPT, reply_markup=keyboards.meds_ask_kb()
        )


@router.callback_query(F.data.in_({"m:med:no", "m:med:skip"}))
async def cb_med_no_skip(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    """«Нет» / «Пропустил»: флаг FALSE / NULL (§6.3) — назад к кнопкам записи."""
    if await _measurement_id(callback, state) is None:
        return
    data = await state.get_data()
    value = False if callback.data == "m:med:no" else None
    tg_id = callback.from_user.id
    user = await measurements_service.get_or_create_user(session, tg_id)
    await measurements_service.set_medication_taken(session, user, data[M_ID], value)
    await session.commit()
    await _hub(callback.message, data)  # type: ignore[arg-type]
    await callback.answer()


async def _should_ask_meds(session: AsyncSession, user: User) -> bool:
    """Мягкая подсказка о лекарствах — один раз, после первой «Да» (§6.3).

    Условие: лекарства ещё не добавлены и это первая запись с флагом «Да».
    """
    if await medications_service.list_medications(session, user):
        return False
    return await measurements_service.medication_taken_count(session, user) == 1


@router.callback_query(F.data == "m:medask:dismiss")
async def cb_meds_ask_dismiss(callback: CallbackQuery) -> None:
    """«Не сейчас» в подсказке о лекарствах — подсказка убирается."""
    await callback.message.edit_text(  # type: ignore[union-attr]
        texts.MEDS_ASK_DISMISSED, reply_markup=None
    )
    await callback.answer()


# ─────────────────────────── Самочувствие ───────────────────────────


@router.callback_query(F.data == "m:feeling")
async def cb_feeling(callback: CallbackQuery, state: FSMContext) -> None:
    if await _measurement_id(callback, state) is None:
        return
    await callback.message.edit_text(  # type: ignore[union-attr]
        texts.FEELING_ASK, reply_markup=keyboards.feeling_kb()
    )
    await callback.answer()


@router.callback_query(
    F.data.in_({"m:feeling:normal", "m:feeling:headache", "m:feeling:weakness"})
)
async def cb_feeling_set(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    if await _measurement_id(callback, state) is None:
        return
    data = await state.get_data()
    feeling = callback.data.split(":", 2)[2]  # type: ignore[union-attr]
    tg_id = callback.from_user.id
    user = await measurements_service.get_or_create_user(session, tg_id)
    await measurements_service.set_feeling(session, user, data[M_ID], feeling)  # type: ignore[arg-type]
    await session.commit()
    await _hub(callback.message, data)  # type: ignore[arg-type]
    await callback.answer()


@router.callback_query(F.data == "m:feeling:other")
async def cb_feeling_other(callback: CallbackQuery, state: FSMContext) -> None:
    """«Другое» — короткий текст, сохранится в note (§6.3)."""
    if await _measurement_id(callback, state) is None:
        return
    await state.set_state(MeasureInput.feeling_other)
    await callback.message.edit_text(  # type: ignore[union-attr]
        texts.PROMPT_FEELING_OTHER, reply_markup=keyboards.note_cancel_kb()
    )
    await callback.answer()


@router.message(MeasureInput.feeling_other)
async def msg_feeling_other(
    message: Message, state: FSMContext, session: AsyncSession
) -> None:
    try:
        other_text = schemas.validate_note(message.text or "")
    except ValueError as exc:
        await message.answer(str(exc), reply_markup=keyboards.note_cancel_kb())
        return
    data = await state.get_data()
    tg_id = message.from_user.id  # type: ignore[union-attr]
    user = await measurements_service.get_or_create_user(session, tg_id)
    await measurements_service.set_feeling(
        session, user, data[M_ID], "other", other_text=other_text
    )
    await session.commit()
    await state.set_state(None)
    await message.answer(texts.NOTE_SAVED, reply_markup=keyboards.saved_kb())


# ─────────────────────────── «Не надо» (отмена повтора) ───────────────────────────


@router.callback_query(F.data.startswith("m:noremeasure:"))
async def cb_no_remeasure(
    callback: CallbackQuery,
    session: AsyncSession,
    reminders: RemindersService,
) -> None:
    """«Не надо»: отменить повторный замер записи (§6.3); id — в callback_data.

    Проверка владельца — в сервисе (фильтр по `user_id`); повторное нажатие
    и чужой id — идемпотентное подтверждение без чужих данных.
    """
    value = callback.data.split(":", 2)[2]  # type: ignore[union-attr]
    if not value.isdigit():
        await _to_menu(callback)
        return
    measurement_id = int(value)
    tg_id = callback.from_user.id
    user = await measurements_service.get_or_create_user(session, tg_id)
    await reminders.cancel_remeasure_for_measurement(user.id, measurement_id)
    await callback.message.edit_text(  # type: ignore[union-attr]
        texts.MED_REMEASURE_CANCELLED, reply_markup=keyboards.saved_kb()
    )
    await callback.answer()
