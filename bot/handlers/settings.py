"""Хендлеры настроек (SPEC.md, §6.6): цели, напоминания, лекарства, часовой пояс,
архивные записи (экран раздела, §6.10).

Inline-меню: каждая опция редактирует сообщение (`edit_message_text`), ответы
на свободный ввод — новыми сообщениями. Слоты вкл/выкл и время идут через
`RemindersService` (строки `reminders` — источник истины, §6.7). Добавление и
изменение лекарства — FSM-цепочка из трёх шагов (название → дозировка → слоты);
изменение и удаление действуют по группе строк лекарства (§6.6).
Экспорт CSV переиспользует экран выбора периода (кнопки `csv:*` обрабатывают
хендлеры статистики).
"""
from __future__ import annotations

from typing import Mapping, Optional, Sequence

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


#: Ключи FSM-data: шаги добавления и изменения лекарства.
M_NAME = "med_name"
M_DOSAGE = "med_dosage"
M_SLOTS = "med_slots"
#: Исходное название группы при правке: по наличию ключа определяется режим
#: правки (канон `m_edit_id`, §8), по значению — группа строк (§6.6).
M_EDIT_NAME = "med_edit_name"

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
        return texts.MEDS_LIST_EMPTY, keyboards.medications_list_kb()
    lines = [
        texts.MEDS_ITEM.format(
            med=texts.fmt_med_item(row.name, row.dosage),
            slots=texts.SLOT_LABELS[row.slot],
        )
        for row in rows
    ]
    text = "\n".join([texts.MEDS_LIST_HEADER, *lines])
    return text, keyboards.medications_list_kb(True)


def _med_pick_screen(
    action: str, groups: Sequence[medications_service.MedicationGroup]
) -> tuple[str, InlineKeyboardMarkup]:
    """Экран выбора лекарства для «Изменить» / «Удалить» (§6.6)."""
    items = [(group.id, group.name, group.dosage) for group in groups]
    prompt = texts.MEDS_EDIT_PICK if action == "med:edit" else texts.MEDS_DEL_PICK
    return prompt, keyboards.medications_pick_kb(action, items)


def _dosage_screen(data: Mapping) -> tuple[str, InlineKeyboardMarkup]:
    """Шаг дозировки: при правке — текущее значение и «Оставить как есть» (§6.6)."""
    if M_EDIT_NAME in data:
        current = data.get(M_DOSAGE) or texts.BTN_NO_DOSAGE
        return (
            texts.MEDS_EDIT_DOSAGE_PROMPT.format(current=current),
            keyboards.dosage_kb(keep=True),
        )
    return texts.MEDS_ADD_DOSAGE_PROMPT, keyboards.dosage_kb()


def _slots_screen(data: Mapping) -> tuple[str, InlineKeyboardMarkup]:
    """Шаг слотов: при правке — с текущим выбором («Сейчас: …», §6.6)."""
    selected = data.get(M_SLOTS, [])
    if M_EDIT_NAME not in data:
        return texts.MEDS_ADD_SLOTS_PROMPT, keyboards.slots_multiselect_kb(selected)
    chosen = [s for s in keyboards.MED_SLOT_ORDER if s in selected]
    current = ", ".join(texts.SLOT_LABELS[s] for s in chosen) or "—"
    return (
        texts.MEDS_EDIT_SLOTS_PROMPT.format(slots=current),
        keyboards.slots_multiselect_kb(selected),
    )


async def _soft_to_list(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession
) -> None:
    """Мягкий откат к списку: устаревшие или чужие кнопки, без лишних сообщений."""
    await state.clear()
    user = await measurements_service.get_or_create_user(session, callback.from_user.id)
    text, markup = await _meds_screen(session, user)
    await callback.message.edit_text(text, reply_markup=markup)  # type: ignore[union-attr]
    await callback.answer()


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


# ─────────────────── Архивные записи (SPEC.md, §6.10) ───────────────────


@router.callback_query(F.data == "set:archive")
async def cb_archive(callback: CallbackQuery) -> None:
    """«Архивные записи» — экран с двумя действиями (§6.6, §6.10).

    Шаги ввода живут в `bot/handlers/measure.py` (кнопки `set:archive:add`,
    `set:archive:quick`); здесь — только корневой экран раздела.
    """
    await callback.message.edit_text(  # type: ignore[union-attr]
        texts.ARCHIVE_MENU, reply_markup=keyboards.archive_menu_kb()
    )
    await callback.answer()


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
async def msg_med_name(
    message: Message, state: FSMContext, session: AsyncSession
) -> None:
    """Название: свободный ввод; при правке — и «Оставить как есть» (§6.6)."""
    data = await state.get_data()
    is_edit = M_EDIT_NAME in data
    step_markup = (
        keyboards.med_name_input_kb()
        if is_edit
        else keyboards.input_cancel_kb("med:cancel")
    )
    try:
        name = schemas.validate_medication_name(message.text or "")
    except ValueError as exc:
        await message.answer(str(exc), reply_markup=step_markup)
        return
    if is_edit and name != data.get(M_EDIT_NAME):
        user = await measurements_service.get_or_create_user(
            session, message.from_user.id
        )
        if await medications_service.medications_by_name(session, user, name):
            await message.answer(texts.MEDS_EDIT_NAME_TAKEN, reply_markup=step_markup)
            return
    if is_edit:
        await state.update_data(**{M_NAME: name})  # слоты уже предзаполнены
    else:
        await state.update_data(**{M_NAME: name, M_SLOTS: []})
    await state.set_state(SettingsInput.med_dosage)
    prompt, markup = _dosage_screen(await state.get_data())
    await message.answer(prompt, reply_markup=markup)


@router.callback_query(F.data == "med:name:same")
async def cb_med_name_keep(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession
) -> None:
    """«Оставить как есть» на шаге названия — без свободного ввода (§6.6)."""
    data = await state.get_data()
    edit_name = data.get(M_EDIT_NAME)
    if not edit_name:  # устаревшие кнопки без черновика правки
        await _soft_to_list(callback, state, session)
        return
    await state.update_data(**{M_NAME: edit_name})
    await state.set_state(SettingsInput.med_dosage)
    prompt, markup = _dosage_screen(await state.get_data())
    await callback.message.edit_text(prompt, reply_markup=markup)  # type: ignore[union-attr]
    await callback.answer()


@router.callback_query(F.data == "med:dose:other")
async def cb_med_dose_other(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(SettingsInput.med_dosage)
    await callback.message.edit_text(  # type: ignore[union-attr]
        texts.MEDS_ADD_DOSAGE_OTHER_PROMPT,
        reply_markup=keyboards.input_cancel_kb("med:cancel"),
    )
    await callback.answer()


@router.callback_query(F.data == "med:dose:none")
async def cb_med_dose_none(callback: CallbackQuery, state: FSMContext) -> None:
    """«Без дозировки» — пустая дозировка, дальше выбор слотов."""
    await state.update_data(**{M_DOSAGE: ""})
    await state.set_state(None)  # дальше — только кнопки слотов
    data = await state.get_data()
    prompt, markup = _slots_screen(data)
    await callback.message.edit_text(prompt, reply_markup=markup)  # type: ignore[union-attr]
    await callback.answer()


@router.callback_query(F.data.in_(keyboards.DOSAGE_CALLBACKS))
async def cb_med_dose(callback: CallbackQuery, state: FSMContext) -> None:
    dosage = callback.data.split(":", 2)[2]  # type: ignore[union-attr]
    await state.update_data(**{M_DOSAGE: dosage})
    await state.set_state(None)  # дальше — только кнопки слотов
    data = await state.get_data()
    prompt, markup = _slots_screen(data)
    await callback.message.edit_text(prompt, reply_markup=markup)  # type: ignore[union-attr]
    await callback.answer()


@router.callback_query(F.data == "med:dose:same")
async def cb_med_dose_keep(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession
) -> None:
    """«Оставить как есть» на шаге дозировки — дозировка прежняя (§6.6)."""
    data = await state.get_data()
    if not data.get(M_EDIT_NAME):  # устаревшие кнопки без черновика правки
        await _soft_to_list(callback, state, session)
        return
    await state.set_state(None)  # дальше — только кнопки слотов
    data = await state.get_data()
    prompt, markup = _slots_screen(data)
    await callback.message.edit_text(prompt, reply_markup=markup)  # type: ignore[union-attr]
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
    prompt, markup = _slots_screen(data)
    await message.answer(prompt, reply_markup=markup)


@router.callback_query(F.data.startswith("med:slot:"))
async def cb_med_slot(callback: CallbackQuery, state: FSMContext) -> None:
    """Мультивыбор слотов: повторное нажатие снимает отметку (§6.6)."""
    slot = callback.data.split(":", 2)[2]  # type: ignore[union-attr]
    if slot not in keyboards.MED_SLOT_ORDER:
        await callback.answer()
        return
    data = await state.get_data()
    selected = list(data.get(M_SLOTS, []))
    if slot in selected:
        selected.remove(slot)
    else:
        selected.append(slot)
    await state.update_data(**{M_SLOTS: selected})
    prompt, markup = _slots_screen({**data, M_SLOTS: selected})
    await callback.message.edit_text(prompt, reply_markup=markup)  # type: ignore[union-attr]
    await callback.answer()


@router.callback_query(F.data == "med:slots:done")
async def cb_med_slots_done(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession
) -> None:
    """«Готово» — сохранить лекарство по выбранным слотам (§6.6)."""
    data = await state.get_data()
    name = data.get(M_NAME)
    edit_name = data.get(M_EDIT_NAME)
    if not name and not edit_name:  # устаревшие кнопки без черновика — мягко к списку
        await _soft_to_list(callback, state, session)
        return
    slots = [s for s in keyboards.MED_SLOT_ORDER if s in data.get(M_SLOTS, [])]
    if not slots:
        await callback.answer(texts.MEDS_ADD_NO_SLOTS)
        return
    user = await measurements_service.get_or_create_user(session, callback.from_user.id)
    if edit_name:
        try:
            updated = await medications_service.update_medication(
                session,
                user,
                edit_name,
                name or edit_name,
                data.get(M_DOSAGE, ""),
                slots,
            )
        except medications_service.MedicationNameConflictError:
            # Гонка: имя заняли, пока шла правка — остаёмся на экране слотов.
            await callback.answer(texts.MEDS_EDIT_NAME_TAKEN)
            return
        if updated is None:  # группу удалили, пока шла правка
            await _soft_to_list(callback, state, session)
            return
        # Только кризис → напоминаний не будет (§6.7).
        done = (
            texts.MEDS_EDIT_DONE
            if any(s in medications_service.SCHEDULED_SLOTS for s in slots)
            else texts.MEDS_EDIT_DONE_CRISIS
        )
    else:
        await medications_service.add_medication(
            session, user, name, data.get(M_DOSAGE, ""), slots
        )
        done = (
            texts.MEDS_ADD_DONE
            if any(s in medications_service.SCHEDULED_SLOTS for s in slots)
            else texts.MEDS_ADD_DONE_CRISIS
        )
    await session.commit()
    await state.clear()
    _, markup = await _meds_screen(session, user)
    await callback.message.edit_text(done, reply_markup=markup)  # type: ignore[union-attr]
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


@router.callback_query(F.data == "med:edit")
async def cb_med_edit_menu(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession
) -> None:
    """«Изменить» — выбор лекарства из списка (§6.6)."""
    await state.clear()
    user = await measurements_service.get_or_create_user(session, callback.from_user.id)
    groups = await medications_service.list_medication_groups(session, user)
    if not groups:  # устаревшая кнопка — лекарств уже нет
        text, markup = await _meds_screen(session, user)
    else:
        text, markup = _med_pick_screen("med:edit", groups)
    await callback.message.edit_text(text, reply_markup=markup)  # type: ignore[union-attr]
    await callback.answer()


@router.callback_query(F.data.startswith("med:edit:"))
async def cb_med_edit_pick(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession
) -> None:
    """Выбор лекарства для правки: черновик + шаг названия (§6.6)."""
    value = callback.data.split(":", 2)[2]  # type: ignore[union-attr]
    user = await measurements_service.get_or_create_user(session, callback.from_user.id)
    medication = await _find_own_medication(session, user, value)
    if medication is None:  # чужое, мусор или уже удалено — мягко к списку
        await _soft_to_list(callback, state, session)
        return
    group = await medications_service.medications_by_name(session, user, medication.name)
    # Черновик правки предзаполняется текущими значениями (канон `m_edit_id`, §8).
    await state.clear()
    await state.update_data(
        **{
            M_EDIT_NAME: medication.name,
            M_NAME: medication.name,
            M_DOSAGE: medication.dosage,
            M_SLOTS: [row.slot for row in group],
        }
    )
    await state.set_state(SettingsInput.med_name)
    await callback.message.edit_text(  # type: ignore[union-attr]
        texts.MEDS_EDIT_NAME_PROMPT.format(name=medication.name),
        reply_markup=keyboards.med_name_input_kb(),
    )
    await callback.answer()


@router.callback_query(F.data == "med:del")
async def cb_med_del_menu(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession
) -> None:
    """«Удалить» — выбор лекарства из списка (§6.6)."""
    await state.clear()
    user = await measurements_service.get_or_create_user(session, callback.from_user.id)
    groups = await medications_service.list_medication_groups(session, user)
    if not groups:  # устаревшая кнопка — лекарств уже нет
        text, markup = await _meds_screen(session, user)
    else:
        text, markup = _med_pick_screen("med:del", groups)
    await callback.message.edit_text(text, reply_markup=markup)  # type: ignore[union-attr]
    await callback.answer()


@router.callback_query(F.data.startswith("med:del:"))
async def cb_med_del_pick(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession
) -> None:
    """Выбор лекарства для удаления: сразу, без подтверждения (§6.6)."""
    value = callback.data.split(":", 2)[2]  # type: ignore[union-attr]
    await state.clear()
    user = await measurements_service.get_or_create_user(session, callback.from_user.id)
    medication = await _find_own_medication(session, user, value)
    if medication is None:  # чужое, мусор или уже удалено
        text, markup = await _meds_screen(session, user)
        await callback.message.edit_text(text, reply_markup=markup)  # type: ignore[union-attr]
        await callback.answer()
        return
    await medications_service.delete_medication_group(session, user, medication.name)
    await session.commit()
    text, markup = await _meds_screen(session, user)
    await callback.message.edit_text(text, reply_markup=markup)  # type: ignore[union-attr]
    await callback.answer(texts.MEDS_DELETED)  # тост: список уже обновлён (§6.6)
