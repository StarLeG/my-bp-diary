"""Хендлеры настроек (SPEC.md, §6.6): цели, напоминания, лекарства, часовой пояс.

Inline-меню: каждая опция редактирует сообщение (`edit_message_text`), ответы
на свободный ввод — новыми сообщениями. Слоты вкл/выкл и время идут через
`RemindersService` (строки `reminders` — источник истины, §6.7). Добавление
лекарства — FSM-цепочка из трёх шагов (название → дозировка → слоты).
Экспорт CSV переиспользует экран выбора периода (кнопки `csv:*` обрабатывают
хендлеры статистики).
"""
from __future__ import annotations

from typing import Optional, Sequence

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message, User
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot import keyboards, schemas, texts
from bot.handlers.start import DEFAULT_SLOT_TIMES, _tz_offset
from bot.models import Medication, Reminder
from bot.services import medications as medications_service
from bot.services import measurements as measurements_service
from bot.services.reminders import RemindersService

router = Router(name="settings")


class SettingsInput(StatesGroup):
    """FSM-состояния свободного ввода настроек (§6.9: «режим ввода»)."""

    target_sys = State()
    target_dia = State()
    slot_time = State()
    tz_input = State()
    med_name = State()
    med_dosage = State()


#: Ключи FSM-data: шаги добавления лекарства.
M_NAME = "med_name"
M_DOSAGE = "med_dosage"
M_SLOTS = "med_slots"

#: Ключи FSM-data: черновики остальных вводов.
T_SLOT = "slot"
T_SYS = "target_sys"


# ─────────────────────────── Экраны ───────────────────────────


def _settings_screen() -> tuple[str, InlineKeyboardMarkup]:
    return texts.SETTINGS_MENU, keyboards.settings_kb()


def _targets_screen() -> tuple[str, InlineKeyboardMarkup]:
    return texts.TARGETS_ASK, keyboards.targets_kb()


async def _reminder_rows(session: AsyncSession, user) -> dict[str, Reminder]:
    """Строки слотов напоминаний пользователя по типу (§4.3)."""
    rows = (
        (
            await session.execute(
                select(Reminder).where(
                    Reminder.user_id == user.id, Reminder.type.in_(keyboards.SLOT_ORDER)
                )
            )
        )
        .scalars()
        .all()
    )
    return {row.type: row for row in rows}


async def _reminders_screen(
    session: AsyncSession, user
) -> tuple[str, InlineKeyboardMarkup]:
    rows = await _reminder_rows(session, user)
    enabled = {slot: bool(rows[slot].enabled) if slot in rows else False for slot in keyboards.SLOT_ORDER}
    times = {
        slot: rows[slot].local_time
        for slot in keyboards.SLOT_ORDER
        if slot in rows and rows[slot].local_time
    }
    return texts.REMINDERS_MENU, keyboards.reminders_settings_kb(
        enabled, times, user.med_remeasure_delay
    )


async def _meds_screen(session: AsyncSession, user) -> tuple[str, InlineKeyboardMarkup]:
    """Список лекарств: строка на каждый слот (§4.4: слот = строка БД)."""
    rows = await medications_service.list_medications(session, user)
    if not rows:
        return texts.MEDS_LIST_EMPTY, keyboards.medications_list_kb([])
    lines = [
        texts.MEDS_ITEM.format(
            name=row.name, dosage=row.dosage, slots=texts.SLOT_LABELS[row.slot]
        )
        for row in rows
    ]
    text = "\n".join([texts.MEDS_LIST_HEADER, *lines])
    items = [(row.id, row.name, row.dosage) for row in rows]
    return text, keyboards.medications_list_kb(items)


async def _saved_target(message, session, user, sys_v: int, dia_v: int, *, via_message: bool) -> None:
    """Сохранить цель и вернуться в настройки (§6.6)."""
    user.target_systolic, user.target_diastolic = sys_v, dia_v
    await session.commit()
    text = texts.TARGETS_SAVED.format(target=f"{sys_v}/{dia_v}")
    _, markup = _settings_screen()
    if via_message:
        await message.answer(text, reply_markup=markup)
    else:
        await message.edit_text(text, reply_markup=markup)  # type: ignore[union-attr]


# ─────────────────────────── Общее меню ───────────────────────────


@router.callback_query(F.data.in_({"menu:settings", "set:back"}))
async def cb_settings(callback: CallbackQuery) -> None:
    """«Настройки» / «Назад» — корневое меню настроек."""
    text, markup = _settings_screen()
    await callback.message.edit_text(text, reply_markup=markup)  # type: ignore[union-attr]
    await callback.answer()


# ─────────────────────────── Целевые значения ───────────────────────────


@router.callback_query(F.data == "set:targets")
async def cb_targets(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    text, markup = _targets_screen()
    await callback.message.edit_text(text, reply_markup=markup)  # type: ignore[union-attr]
    await callback.answer()


@router.callback_query(F.data == "set:target:other")
async def cb_target_other(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(SettingsInput.target_sys)
    await callback.message.edit_text(  # type: ignore[union-attr]
        texts.TARGETS_OTHER_PROMPT, reply_markup=keyboards.input_cancel_kb("set:targets")
    )
    await callback.answer()


@router.callback_query(
    F.data.in_(f"set:target:{key}" for key in keyboards.TARGET_PRESETS)
)
async def cb_target_preset(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession
) -> None:
    key = callback.data.split(":", 2)[2]  # type: ignore[union-attr]
    sys_v, dia_v = keyboards.TARGET_PRESETS[key]
    await state.clear()
    user = await measurements_service.get_or_create_user(session, callback.from_user.id)
    await _saved_target(callback.message, session, user, sys_v, dia_v, via_message=False)
    await callback.answer()


@router.message(SettingsInput.target_sys)
async def msg_target_sys(message: Message, state: FSMContext) -> None:
    try:
        sys_v = schemas.validate_int(message.text or "", 70, 260, "систолическое")
    except ValueError as exc:
        await message.answer(str(exc), reply_markup=keyboards.input_cancel_kb("set:targets"))
        return
    await state.update_data(**{T_SYS: sys_v})
    await state.set_state(SettingsInput.target_dia)
    await message.answer(
        texts.TARGETS_OTHER_PROMPT_DIA, reply_markup=keyboards.input_cancel_kb("set:targets")
    )


@router.message(SettingsInput.target_dia)
async def msg_target_dia(
    message: Message, state: FSMContext, session: AsyncSession
) -> None:
    try:
        dia_v = schemas.validate_int(message.text or "", 40, 160, "диастолическое")
    except ValueError as exc:
        await message.answer(str(exc), reply_markup=keyboards.input_cancel_kb("set:targets"))
        return
    data = await state.get_data()
    tg_user: User = message.from_user  # type: ignore[assignment]
    user = await measurements_service.get_or_create_user(session, tg_user.id)
    # инвариант: шаг «систолическое» уже пройден; fallback — текущая цель
    sys_v = int(data.get(T_SYS) or user.target_systolic)
    await state.clear()
    await _saved_target(message, session, user, sys_v, dia_v, via_message=True)


# ─────────────────────────── Напоминания ───────────────────────────


@router.callback_query(F.data == "set:reminders")
async def cb_reminders(callback: CallbackQuery, session: AsyncSession) -> None:
    user = await measurements_service.get_or_create_user(session, callback.from_user.id)
    text, markup = await _reminders_screen(session, user)
    await callback.message.edit_text(text, reply_markup=markup)  # type: ignore[union-attr]
    await callback.answer()


@router.callback_query(F.data.startswith("set:slot:"))
async def cb_slot_toggle(
    callback: CallbackQuery,
    session: AsyncSession,
    reminders: RemindersService,
) -> None:
    """Клик по слоту: включить (с сохранённым или дефолтным временем) / выключить."""
    slot = callback.data.split(":", 2)[2]  # type: ignore[union-attr]
    if slot not in keyboards.SLOT_ORDER:
        await callback.answer()
        return
    user = await measurements_service.get_or_create_user(session, callback.from_user.id)
    rows = await _reminder_rows(session, user)
    current = rows.get(slot)
    if current is not None and current.enabled:
        await reminders.disable_slot(user.id, slot)
    else:
        local_time = (current.local_time if current and current.local_time else None) or DEFAULT_SLOT_TIMES[slot]
        await reminders.ensure_slot(user.id, slot, local_time, user.timezone)
    text, markup = await _reminders_screen(session, user)
    await callback.message.edit_text(text, reply_markup=markup)  # type: ignore[union-attr]
    await callback.answer()


@router.callback_query(F.data.startswith("set:time:"))
async def cb_slot_time(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    reminders: RemindersService,
) -> None:
    """Время слота: показать варианты / «Другое» (FSM) / выбрать пресет."""
    value = callback.data.split(":", 2)[2]  # type: ignore[union-attr]
    slot, _, action = value.partition(":")
    if slot not in keyboards.SLOT_ORDER:
        await callback.answer()
        return
    if action == "show":
        text = texts.SLOT_TIME_ASK.format(slot=texts.SLOT_LABELS[slot])
        markup = keyboards.slot_time_kb(slot)
    elif action == "other":
        await state.set_state(SettingsInput.slot_time)
        await state.update_data(**{T_SLOT: slot})
        text = texts.SLOT_TIME_ASK.format(slot=texts.SLOT_LABELS[slot])
        markup = keyboards.input_cancel_kb("set:reminders")
    else:
        try:
            moment = schemas.validate_time(action)
        except ValueError as exc:
            await callback.answer(str(exc))
            return
        user = await measurements_service.get_or_create_user(session, callback.from_user.id)
        await state.clear()
        await reminders.ensure_slot(user.id, slot, moment.strftime("%H:%M"), user.timezone)
        text, markup = await _reminders_screen(session, user)
    await callback.message.edit_text(text, reply_markup=markup)  # type: ignore[union-attr]
    await callback.answer()


@router.message(SettingsInput.slot_time)
async def msg_slot_time(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    reminders: RemindersService,
) -> None:
    data = await state.get_data()
    slot = data.get(T_SLOT, "morning")
    try:
        moment = schemas.validate_time(message.text or "")
    except ValueError as exc:
        await message.answer(str(exc), reply_markup=keyboards.input_cancel_kb("set:reminders"))
        return
    tg_user: User = message.from_user  # type: ignore[assignment]
    user = await measurements_service.get_or_create_user(session, tg_user.id)
    await state.clear()
    await reminders.ensure_slot(user.id, slot, moment.strftime("%H:%M"), user.timezone)
    text, markup = await _reminders_screen(session, user)
    await message.answer(text, reply_markup=markup)


@router.callback_query(F.data == "set:remeasure")
async def cb_remeasure_menu(callback: CallbackQuery) -> None:
    await callback.message.edit_text(  # type: ignore[union-attr]
        texts.REMEASURE_ASK, reply_markup=keyboards.remeasure_delay_kb()
    )
    await callback.answer()


@router.callback_query(
    F.data.in_(
        {
            "set:remeasure:off",
            "set:remeasure:30",
            "set:remeasure:60",
            "set:remeasure:120",
        }
    )
)
async def cb_remeasure_set(
    callback: CallbackQuery, session: AsyncSession
) -> None:
    """«Повтор после лекарства»: выкл / 30 / 60 / 120 минут (§4.1, §6.7)."""
    value = callback.data.split(":", 2)[2]  # type: ignore[union-attr]
    user = await measurements_service.get_or_create_user(session, callback.from_user.id)
    if value == "off":
        user.med_remeasure_delay = None
        label = texts.REMEASURE_OFF
    else:
        user.med_remeasure_delay = int(value)
        label = f"{value} минут"
    await session.commit()
    _, markup = await _reminders_screen(session, user)
    await callback.message.edit_text(  # type: ignore[union-attr]
        texts.REMEASURE_SAVED.format(value=label), reply_markup=markup
    )
    await callback.answer()


# ─────────────────────────── Часовой пояс ───────────────────────────


@router.callback_query(F.data == "set:tz")
async def cb_tz_menu(callback: CallbackQuery) -> None:
    await callback.message.edit_text(  # type: ignore[union-attr]
        texts.TZ_ASK, reply_markup=keyboards.timezone_kb()
    )
    await callback.answer()


@router.callback_query(F.data == "set:tz:other")
async def cb_tz_other(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(SettingsInput.tz_input)
    await callback.message.edit_text(  # type: ignore[union-attr]
        texts.TZ_OTHER_PROMPT, reply_markup=keyboards.input_cancel_kb("set:tz")
    )
    await callback.answer()


async def _saved_timezone(message, session, user, tz_name: str, *, via_message: bool) -> None:
    user.timezone = tz_name
    await session.commit()
    text = texts.TZ_SAVED.format(tz=tz_name, offset=_tz_offset(tz_name))
    _, markup = _settings_screen()
    if via_message:
        await message.answer(text, reply_markup=markup)
    else:
        await message.edit_text(text, reply_markup=markup)  # type: ignore[union-attr]


@router.callback_query(F.data.startswith("set:tz:"))
async def cb_tz_set(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession
) -> None:
    value = callback.data.split(":", 2)[2]  # type: ignore[union-attr]
    try:
        tz_name = schemas.validate_timezone(value)
    except ValueError as exc:
        await callback.answer(str(exc))
        return
    user = await measurements_service.get_or_create_user(session, callback.from_user.id)
    await state.clear()
    await _saved_timezone(callback.message, session, user, tz_name, via_message=False)
    await callback.answer()


@router.message(SettingsInput.tz_input)
async def msg_tz(message: Message, state: FSMContext, session: AsyncSession) -> None:
    try:
        tz_name = schemas.validate_timezone(message.text or "")
    except ValueError as exc:
        await message.answer(str(exc), reply_markup=keyboards.input_cancel_kb("set:tz"))
        return
    tg_user: User = message.from_user  # type: ignore[assignment]
    user = await measurements_service.get_or_create_user(session, tg_user.id)
    await state.clear()
    await _saved_timezone(message, session, user, tz_name, via_message=True)


# ─────────────────────────── Экспорт CSV ───────────────────────────


@router.callback_query(F.data == "set:csv")
async def cb_csv(callback: CallbackQuery) -> None:
    """«Экспорт CSV» — тот же экран периода, что в статистике (§6.6, §10)."""
    await callback.message.edit_text(  # type: ignore[union-attr]
        texts.CSV_ASK, reply_markup=keyboards.csv_period_kb()
    )
    await callback.answer()


# ─────────────────────────── Удаление аккаунта (§19) ───────────────────────────


@router.callback_query(F.data == "set:delete")
async def cb_delete_account(callback: CallbackQuery) -> None:
    """«Удалить аккаунт» — предупреждение «Это действие необратимо...» (§19)."""
    await callback.message.edit_text(  # type: ignore[union-attr]
        texts.DELETE_ACCOUNT_WARNING, reply_markup=keyboards.delete_account_kb()
    )
    await callback.answer()


@router.callback_query(F.data == "set:delete:ok")
async def cb_delete_account_confirm(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    reminders: RemindersService,
) -> None:
    """Подтверждение: job'ы → удаление данных (§19). Двойное нажатие безопасно.

    Если пользователя уже нет (повторный клик, гонка) — тот же финальный текст:
    цель действия достигнута, ошибки не показываем.
    """
    await state.clear()
    user = await measurements_service.get_user(session, callback.from_user.id)
    if user is not None:
        await measurements_service.delete_user_data(session, reminders, user)
    await callback.message.edit_text(  # type: ignore[union-attr]
        texts.DELETE_ACCOUNT_DONE, reply_markup=keyboards.back_to_menu()
    )
    await callback.answer()


# ─────────────────────────── Мои лекарства ───────────────────────────


@router.callback_query(F.data == "set:meds")
async def cb_meds(callback: CallbackQuery, session: AsyncSession) -> None:
    user = await measurements_service.get_or_create_user(session, callback.from_user.id)
    text, markup = await _meds_screen(session, user)
    await callback.message.edit_text(text, reply_markup=markup)  # type: ignore[union-attr]
    await callback.answer()


@router.callback_query(F.data == "med:add")
async def cb_med_add(callback: CallbackQuery, state: FSMContext) -> None:
    """«Добавить» — шаг 1: название (§6.6); кнопка есть и в промпте «Напоминать…»."""
    await state.clear()
    await state.set_state(SettingsInput.med_name)
    await callback.message.edit_text(  # type: ignore[union-attr]
        texts.MEDS_ADD_NAME_PROMPT, reply_markup=keyboards.input_cancel_kb("med:cancel")
    )
    await callback.answer()


@router.callback_query(F.data == "med:cancel")
async def cb_med_cancel(callback: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    await state.clear()
    user = await measurements_service.get_or_create_user(session, callback.from_user.id)
    text, markup = await _meds_screen(session, user)
    await callback.message.edit_text(text, reply_markup=markup)  # type: ignore[union-attr]
    await callback.answer()


@router.message(SettingsInput.med_name)
async def msg_med_name(message: Message, state: FSMContext) -> None:
    try:
        name = schemas.validate_medication_name(message.text or "")
    except ValueError as exc:
        await message.answer(str(exc), reply_markup=keyboards.input_cancel_kb("med:cancel"))
        return
    await state.update_data(**{M_NAME: name, M_SLOTS: []})
    await state.set_state(SettingsInput.med_dosage)
    await message.answer(texts.MEDS_ADD_DOSAGE_PROMPT, reply_markup=keyboards.dosage_kb())


@router.callback_query(F.data == "med:dose:other")
async def cb_med_dose_other(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(SettingsInput.med_dosage)
    await callback.message.edit_text(  # type: ignore[union-attr]
        texts.MEDS_ADD_DOSAGE_OTHER_PROMPT,
        reply_markup=keyboards.input_cancel_kb("med:cancel"),
    )
    await callback.answer()


@router.callback_query(F.data.in_(keyboards.DOSAGE_CALLBACKS))
async def cb_med_dose(callback: CallbackQuery, state: FSMContext) -> None:
    dosage = callback.data.split(":", 2)[2]  # type: ignore[union-attr]
    await state.update_data(**{M_DOSAGE: dosage})
    await state.set_state(None)  # дальше — только кнопки слотов
    data = await state.get_data()
    text, markup = (
        texts.MEDS_ADD_SLOTS_PROMPT,
        keyboards.slots_multiselect_kb(data.get(M_SLOTS, [])),
    )
    await callback.message.edit_text(text, reply_markup=markup)  # type: ignore[union-attr]
    await callback.answer()


@router.message(SettingsInput.med_dosage)
async def msg_med_dosage(message: Message, state: FSMContext) -> None:
    try:
        dosage = schemas.validate_dosage(message.text or "")
    except ValueError as exc:
        await message.answer(str(exc), reply_markup=keyboards.input_cancel_kb("med:cancel"))
        return
    await state.update_data(**{M_DOSAGE: dosage})
    await state.set_state(None)  # дальше — только кнопки слотов
    data = await state.get_data()
    await message.answer(
        texts.MEDS_ADD_SLOTS_PROMPT,
        reply_markup=keyboards.slots_multiselect_kb(data.get(M_SLOTS, [])),
    )


@router.callback_query(F.data.startswith("med:slot:"))
async def cb_med_slot(callback: CallbackQuery, state: FSMContext) -> None:
    """Мультивыбор слотов: повторное нажатие снимает отметку (§6.6)."""
    slot = callback.data.split(":", 2)[2]  # type: ignore[union-attr]
    if slot not in keyboards.SLOT_ORDER:
        await callback.answer()
        return
    data = await state.get_data()
    selected = list(data.get(M_SLOTS, []))
    if slot in selected:
        selected.remove(slot)
    else:
        selected.append(slot)
    await state.update_data(**{M_SLOTS: selected})
    await callback.message.edit_text(  # type: ignore[union-attr]
        texts.MEDS_ADD_SLOTS_PROMPT, reply_markup=keyboards.slots_multiselect_kb(selected)
    )
    await callback.answer()


@router.callback_query(F.data == "med:slots:done")
async def cb_med_slots_done(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession
) -> None:
    """«Готово» — сохранить лекарство по выбранным слотам (§6.6)."""
    data = await state.get_data()
    name = data.get(M_NAME)
    if not name:  # устаревшие кнопки без черновика — мягко к списку
        await state.clear()
        user = await measurements_service.get_or_create_user(session, callback.from_user.id)
        text, markup = await _meds_screen(session, user)
        await callback.message.edit_text(text, reply_markup=markup)  # type: ignore[union-attr]
        await callback.answer()
        return
    slots = [s for s in keyboards.SLOT_ORDER if s in data.get(M_SLOTS, [])]
    if not slots:
        await callback.answer(texts.MEDS_ADD_NO_SLOTS)
        return
    user = await measurements_service.get_or_create_user(session, callback.from_user.id)
    await medications_service.add_medication(
        session, user, name, data.get(M_DOSAGE, ""), slots
    )
    await session.commit()
    await state.clear()
    _, markup = await _meds_screen(session, user)
    await callback.message.edit_text(texts.MEDS_ADD_DONE, reply_markup=markup)  # type: ignore[union-attr]
    await callback.answer()


async def _find_own_medication(
    session: AsyncSession, user, raw_value: str
) -> Optional[Medication]:
    """Лекарство по id из callback; чужое или мусор — как отсутствующее (§7.1)."""
    if not raw_value.isdigit():
        return None
    return (
        await session.execute(
            select(Medication).where(
                Medication.id == int(raw_value), Medication.user_id == user.id
            )
        )
    ).scalar_one_or_none()


@router.callback_query(F.data.startswith("med:del:"))
async def cb_med_delete(callback: CallbackQuery, session: AsyncSession) -> None:
    """«Удалить» — подтверждение с именем препарата (§6.6)."""
    value = callback.data.split(":", 2)[2]  # type: ignore[union-attr]
    user = await measurements_service.get_or_create_user(session, callback.from_user.id)
    medication = await _find_own_medication(session, user, value)
    if medication is None:
        text, markup = await _meds_screen(session, user)
        await callback.message.edit_text(text, reply_markup=markup)  # type: ignore[union-attr]
        await callback.answer()
        return
    await callback.message.edit_text(  # type: ignore[union-attr]
        texts.MEDS_CONFIRM_DELETE.format(name=medication.name, dosage=medication.dosage),
        reply_markup=keyboards.med_confirm_delete_kb(medication.id),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("med:delok:"))
async def cb_med_delete_confirm(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession
) -> None:
    value = callback.data.split(":", 2)[2]  # type: ignore[union-attr]
    await state.clear()
    user = await measurements_service.get_or_create_user(session, callback.from_user.id)
    deleted = (
        await medications_service.delete_medication(session, user, int(value))
        if value.isdigit()
        else False
    )
    await session.commit()
    text, markup = await _meds_screen(session, user)
    if deleted:
        text = texts.MEDS_DELETED
    await callback.message.edit_text(text, reply_markup=markup)  # type: ignore[union-attr]
    await callback.answer()
