"""Хендлеры /start: онбординг и главное меню (SPEC.md, §6.1–6.2).

Онбординг — цепочка экранов с кнопками (приветствие → режим отображения →
часовой пояс → цели → мультивыбор слотов → время слотов → «Готово»). Шаг 2
«Как вам удобнее смотреть?» (UI7): `onb:ui:0|1|skip`; «Крупный» перерисовывает
экран с «✓» (демонстрация режима), повторное нажатие — дальше; `skip` — обычный.
Шаги 3–4 отрисовываются в выбранном режиме (`users.ui_large`). «Другое» —
свободный ввод (FSM); прерванный онбординг при следующем /start начинается
заново (state.clear()). «Пропустить» принимает значения по умолчанию
(DEFAULT_TIMEZONE, 130/80, время слота по умолчанию). Финал включает напоминания
выбранных слотов (`RemindersService.ensure_slot`), ставит `users.onboarded = 1`
и показывает главное меню. Тексты экранов редактируются (`edit_message_text`),
ответы на свободный ввод — новыми сообщениями.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message, User
from sqlalchemy.ext.asyncio import AsyncSession

from bot import keyboards, schemas, texts
from bot.config import DEFAULT_TIMEZONE
from bot.services import consent as consent_service
from bot.services import doctor as doctor_service
from bot.services import measurements as measurements_service
from bot.services.reminders import DEFAULT_SLOT_TIMES, RemindersService

router = Router(name="start")

#: Дефолт целевых значений при «Пропустить» (§6.1, §4.1).
DEFAULT_TARGET = (130, 80)


class Onboarding(StatesGroup):
    """FSM-состояния свободного ввода онбординга."""

    tz_input = State()
    target_sys = State()
    target_dia = State()
    time_input = State()


# ─────────────────────────── Экраны ───────────────────────────


def _tz_offset(tz_name: str) -> str:
    """Смещение от UTC в формате ±ЧЧ:ММ (§6.1: «показывается смещение от UTC»)."""
    offset = datetime.now(ZoneInfo(tz_name)).utcoffset() or timedelta(0)
    total = int(offset.total_seconds()) // 60
    sign = "+" if total >= 0 else "-"
    return f"{sign}{abs(total) // 60:02d}:{abs(total) % 60:02d}"


def _welcome_screen() -> tuple[str, InlineKeyboardMarkup]:
    return texts.WELCOME, keyboards.onboarding_start_kb()


def _ui_screen(selected: Optional[str] = None) -> tuple[str, InlineKeyboardMarkup]:
    """Шаг 2 (UI7): «Как вам удобнее смотреть?»; `selected` — «✓ » у варианта."""
    return texts.ONB_UI_ASK, keyboards.onboarding_ui_kb(selected)


def _tz_screen() -> tuple[str, InlineKeyboardMarkup]:
    return texts.ONB_TZ_ASK, keyboards.onboarding_timezone_kb()


def _targets_screen(tz_name: str, *, large: bool = False) -> tuple[str, InlineKeyboardMarkup]:
    confirm = texts.fmt_onb_tz_confirm(tz_name, _tz_offset(tz_name), large=large)
    return f"{confirm}\n\n{texts.ONB_TARGETS_ASK}", keyboards.onboarding_targets_kb()


def _slots_screen(
    target: tuple[int, int], selected: list[str], *, large: bool = False
) -> tuple[str, InlineKeyboardMarkup]:
    confirm = texts.fmt_onb_target_confirm(f"{target[0]}/{target[1]}", large=large)
    body = f"{confirm}\n\n{texts.ONB_SLOTS_ASK}\n\n{texts.ONB_REMEASURE_NOTE}"
    return body, keyboards.onboarding_slots_kb(selected)


def _time_screen(slot: str) -> tuple[str, InlineKeyboardMarkup]:
    label = texts.SLOT_LABELS[slot]
    return texts.ONB_TIMES_ASK.format(slot=label), keyboards.onboarding_time_kb(slot)


# ─────────────────────────── /start ───────────────────────────


@router.message(CommandStart())
async def cmd_start(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    """Первый /start — онбординг; повторный — главное меню (§6.1).

    Врач (§21, N5): не прошёл онбординг пациента — меню врача (строка
    `users` не создаётся); прошёл — меню пациента с кнопкой «🩺 Кабинет врача».
    """
    current = await state.get_state()
    await state.clear()
    tg_user: User = message.from_user  # type: ignore[assignment]
    doctor = await doctor_service.doctor_by_telegram(session, tg_user.id)
    if doctor is not None:
        existing = await measurements_service.get_user(session, tg_user.id)
        if existing is None or not existing.onboarded:
            await message.answer(
                texts.DOCTOR_PANEL, reply_markup=keyboards.doctor_panel_kb(show_diary=True)
            )
            return
    user = await measurements_service.get_or_create_user(session, tg_user.id)
    await session.commit()
    if not await consent_service.has_processing(session, user.id):
        # Шаг 0 (§6.1, §6.11): без согласия онбординг не начинается.
        from bot.handlers.consent import consent_screen

        text, markup = consent_screen(existing=user.onboarded)
        await message.answer(
            text, reply_markup=markup, link_preview_options=keyboards.NO_LINK_PREVIEW
        )
        return
    if user.onboarded:
        from bot.handlers.consent import next_confirm

        screen = await next_confirm(session, user.id)
        if screen is not None:  # связь с врачом без согласия на него (Д4)
            await message.answer(screen[0], reply_markup=screen[1])
            return
        await message.answer(
            texts.MAIN_MENU, reply_markup=keyboards.main_menu(is_doctor=doctor is not None)
        )
        return
    if current is not None and current.startswith(f"{Onboarding.__name__}:"):
        text, markup = f"{texts.ONB_RESTARTED}\n\n{texts.WELCOME}", keyboards.onboarding_start_kb()
    else:
        text, markup = _welcome_screen()
    await message.answer(text, reply_markup=markup)


@router.callback_query(F.data == "menu:back")
async def cb_menu_back(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession
) -> None:
    """«В меню» / «Назад» — главное меню (сообщение редактируется, §6.2).

    Врачу — с кнопкой «🩺 Кабинет врача» (§21, N5).
    """
    await state.clear()
    is_doctor = await doctor_service.doctor_by_telegram(session, callback.from_user.id) is not None
    text, markup = texts.MAIN_MENU, keyboards.main_menu(is_doctor=is_doctor)
    await callback.message.edit_text(text, reply_markup=markup)  # type: ignore[union-attr]
    await callback.answer()


# ─────────────────────────── Шаг 1: приветствие ───────────────────────────


@router.callback_query(F.data == "onb:start")
async def cb_onb_start(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession
) -> None:
    """«Начать»: выбор режима, дальше часовой пояс; без согласия — снова экран согласия (§6.11)."""
    await state.clear()
    user = await measurements_service.get_or_create_user(session, callback.from_user.id)
    if not await consent_service.has_processing(session, user.id):
        from bot.handlers.consent import consent_screen

        text, markup = consent_screen(existing=user.onboarded)
        await callback.message.edit_text(  # type: ignore[union-attr]
            text, reply_markup=markup, link_preview_options=keyboards.NO_LINK_PREVIEW
        )
        await callback.answer(texts.CONSENT_REQUIRED)
        return
    text, markup = _ui_screen()
    await callback.message.edit_text(text, reply_markup=markup)  # type: ignore[union-attr]
    await callback.answer()


# ─────────────────── Шаг 2: режим отображения (UI7, 0.5.10) ───────────────────


@router.callback_query(F.data.in_({"onb:ui:0", "onb:ui:1", "onb:ui:skip"}))
async def cb_onb_ui(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession
) -> None:
    """Выбор режима отображения (§6.1 шаг 2, UI7).

    `onb:ui:skip` — обычный режим; `onb:ui:0` — обычный и сразу шаг 3; `onb:ui:1`
    при первом нажатии перерисовывает этот же экран крупным с «✓» (демонстрация),
    повторное нажатие — шаг 3. `users.ui_large` переключается сразу (§8).
    """
    value = (callback.data or "").split(":")[2]
    user = await measurements_service.get_or_create_user(session, callback.from_user.id)
    if value == "1" and (await state.get_data()).get("onb_ui") != "1":
        await state.update_data(onb_ui="1")
        user.ui_large = True
        await session.commit()
        text, markup = _ui_screen("1")
        await callback.message.edit_text(text, reply_markup=markup)  # type: ignore[union-attr]
        await callback.answer()
        return
    large = value == "1"
    await state.update_data(onb_ui="1" if large else "0")
    user.ui_large = large
    await session.commit()
    text, markup = _tz_screen()
    await callback.message.edit_text(text, reply_markup=markup)  # type: ignore[union-attr]
    await callback.answer()


# ─────────────────────────── Шаг 2: часовой пояс ───────────────────────────


@router.callback_query(F.data.startswith("onb:tz:"))
async def cb_onb_tz(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession
) -> None:
    value = callback.data.split(":", 2)[2]  # type: ignore[union-attr]
    if value == "other":
        await state.set_state(Onboarding.tz_input)
        text, markup = texts.ONB_TZ_OTHER_PROMPT, keyboards.onboarding_skip_kb("tz")
    else:
        await state.update_data(onb_tz=value)
        user = await measurements_service.get_or_create_user(session, callback.from_user.id)
        text, markup = _targets_screen(value, large=user.ui_large)
    await callback.message.edit_text(text, reply_markup=markup)  # type: ignore[union-attr]
    await callback.answer()


@router.message(Onboarding.tz_input)
async def msg_onb_tz(
    message: Message, state: FSMContext, session: AsyncSession
) -> None:
    try:
        tz_name = schemas.validate_timezone(message.text or "")
    except ValueError as exc:
        await message.answer(str(exc), reply_markup=keyboards.onboarding_skip_kb("tz"))
        return
    await state.update_data(onb_tz=tz_name)
    await state.set_state(None)
    tg_user: User = message.from_user  # type: ignore[assignment]
    user = await measurements_service.get_or_create_user(session, tg_user.id)
    text, markup = _targets_screen(tz_name, large=user.ui_large)
    await message.answer(text, reply_markup=markup)


# ─────────────────────────── Шаг 3: цели и слоты ───────────────────────────


@router.callback_query(F.data.startswith("onb:target:"))
async def cb_onb_target(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession
) -> None:
    value = callback.data.split(":", 2)[2]  # type: ignore[union-attr]
    if value == "other":
        await state.set_state(Onboarding.target_sys)
        text = texts.ONB_TARGETS_OTHER_PROMPT
        markup = keyboards.onboarding_skip_kb("targets")
    else:
        target = keyboards.TARGET_PRESETS[value]
        await state.update_data(onb_target=target)
        user = await measurements_service.get_or_create_user(session, callback.from_user.id)
        text, markup = _slots_screen(target, [], large=user.ui_large)
    await callback.message.edit_text(text, reply_markup=markup)  # type: ignore[union-attr]
    await callback.answer()


@router.message(Onboarding.target_sys)
async def msg_onb_target_sys(message: Message, state: FSMContext) -> None:
    try:
        sys_v = schemas.validate_int(message.text or "", 70, 260, "систолическое")
    except ValueError as exc:
        await message.answer(str(exc), reply_markup=keyboards.onboarding_skip_kb("targets"))
        return
    await state.update_data(onb_target_sys=sys_v)
    await state.set_state(Onboarding.target_dia)
    await message.answer(
        texts.ONB_TARGETS_OTHER_PROMPT_DIA,
        reply_markup=keyboards.onboarding_skip_kb("targets"),
    )


@router.message(Onboarding.target_dia)
async def msg_onb_target_dia(
    message: Message, state: FSMContext, session: AsyncSession
) -> None:
    try:
        dia_v = schemas.validate_int(message.text or "", 40, 160, "диастолическое")
    except ValueError as exc:
        await message.answer(str(exc), reply_markup=keyboards.onboarding_skip_kb("targets"))
        return
    data = await state.get_data()
    target = (data.get("onb_target_sys", DEFAULT_TARGET[0]), dia_v)
    await state.update_data(onb_target=target)
    await state.set_state(None)
    tg_user: User = message.from_user  # type: ignore[assignment]
    user = await measurements_service.get_or_create_user(session, tg_user.id)
    text, markup = _slots_screen(target, [], large=user.ui_large)
    await message.answer(text, reply_markup=markup)


@router.callback_query(F.data.startswith("onb:slot:"))
async def cb_onb_slot(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession
) -> None:
    """Мультивыбор слотов: повторное нажатие снимает отметку."""
    slot = callback.data.split(":", 2)[2]  # type: ignore[union-attr]
    data = await state.get_data()
    selected = list(data.get("onb_slots", []))
    if slot in selected:
        selected.remove(slot)
    else:
        selected.append(slot)
    await state.update_data(onb_slots=selected)
    target = data.get("onb_target", DEFAULT_TARGET)
    user = await measurements_service.get_or_create_user(session, callback.from_user.id)
    text, markup = _slots_screen(tuple(target), selected, large=user.ui_large)
    await callback.message.edit_text(text, reply_markup=markup)  # type: ignore[union-attr]
    await callback.answer()


@router.callback_query(F.data == "onb:slots:none")
async def cb_onb_slots_none(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    reminders: RemindersService,
) -> None:
    """«Без напоминаний» — сразу финал, слоты не создаются (§6.1)."""
    await state.update_data(onb_slots=[], onb_times={})
    await _finish(callback.message, state, session, reminders, callback.from_user.id)  # type: ignore[arg-type]
    await callback.answer()


@router.callback_query(F.data == "onb:slots:done")
async def cb_onb_slots_done(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    reminders: RemindersService,
) -> None:
    """«Готово» — время для каждого выбранного слота, затем финал (§6.1)."""
    data = await state.get_data()
    selected = list(data.get("onb_slots", []))
    times = data.get("onb_times", {})
    pending = [s for s in keyboards.SLOT_ORDER if s in selected and s not in times]
    if pending:
        await state.update_data(onb_pending_slot=pending[0])
        text, markup = _time_screen(pending[0])
        await callback.message.edit_text(text, reply_markup=markup)  # type: ignore[union-attr]
    else:
        await _finish(callback.message, state, session, reminders, callback.from_user.id)  # type: ignore[arg-type]
    await callback.answer()


# ─────────────────────────── Шаг 4: время слота ───────────────────────────


@router.callback_query(F.data.startswith("onb:time:"))
async def cb_onb_time(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    reminders: RemindersService,
) -> None:
    slot, _, action = callback.data.split(":", 2)[2].partition(":")  # type: ignore[union-attr]
    if action == "other":
        await state.set_state(Onboarding.time_input)
        await state.update_data(onb_pending_slot=slot)
        await callback.message.edit_text(  # type: ignore[union-attr]
            texts.ONB_TIME_OTHER_PROMPT,
            reply_markup=keyboards.onboarding_skip_kb(f"time:{slot}"),
        )
        await callback.answer()
        return
    value = DEFAULT_SLOT_TIMES[slot] if action == "skip" else action
    await _save_slot_time(
        callback.message, state, session, reminders, callback.from_user.id, slot, value
    )  # type: ignore[arg-type]
    await callback.answer()


@router.message(Onboarding.time_input)
async def msg_onb_time(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    reminders: RemindersService,
) -> None:
    data = await state.get_data()
    slot = data.get("onb_pending_slot", "morning")
    try:
        moment = schemas.validate_time(message.text or "")
    except ValueError as exc:
        await message.answer(str(exc), reply_markup=keyboards.onboarding_skip_kb(f"time:{slot}"))
        return
    await state.set_state(None)
    tg_user: User = message.from_user  # type: ignore[assignment]
    await _save_slot_time(
        message,
        state,
        session,
        reminders,
        tg_user.id,
        slot,
        moment.strftime("%H:%M"),
        via_message=True,
    )


# ─────────────────────────── Пропустить ───────────────────────────


@router.callback_query(F.data.startswith("onb:skip:"))
async def cb_onb_skip(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    reminders: RemindersService,
) -> None:
    """«Пропустить» — принимаются значения по умолчанию (§6.1)."""
    target = callback.data.split(":", 2)[2]  # type: ignore[union-attr]
    user = await measurements_service.get_or_create_user(session, callback.from_user.id)
    if target == "tz":
        await state.update_data(onb_tz=DEFAULT_TIMEZONE)
        text, markup = _targets_screen(DEFAULT_TIMEZONE, large=user.ui_large)
        await callback.message.edit_text(text, reply_markup=markup)  # type: ignore[union-attr]
    elif target == "targets":
        await state.update_data(onb_target=DEFAULT_TARGET)
        text, markup = _slots_screen(
            DEFAULT_TARGET,
            list((await state.get_data()).get("onb_slots", [])),
            large=user.ui_large,
        )
        await callback.message.edit_text(text, reply_markup=markup)  # type: ignore[union-attr]
    elif target.startswith("time:"):
        slot = target.partition(":")[2]
        await _save_slot_time(
            callback.message,
            state,
            session,
            reminders,
            callback.from_user.id,
            slot,
            DEFAULT_SLOT_TIMES[slot],
        )  # type: ignore[arg-type]
    await state.set_state(None)
    await callback.answer()


# ─────────────────────────── Внутренняя логика ───────────────────────────


async def _save_slot_time(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    reminders: RemindersService,
    telegram_id: int,
    slot: str,
    value: str,
    *,
    via_message: bool = False,
) -> None:
    """Запомнить время слота и показать следующий шаг (или финал)."""
    data = await state.get_data()
    times = dict(data.get("onb_times", {}))
    times[slot] = value
    await state.update_data(onb_times=times)
    selected = list(data.get("onb_slots", []))
    pending = [s for s in keyboards.SLOT_ORDER if s in selected and s not in times]
    if pending:
        await state.update_data(onb_pending_slot=pending[0])
        text, markup = _time_screen(pending[0])
        if via_message:
            await message.answer(text, reply_markup=markup)
        else:
            await message.edit_text(text, reply_markup=markup)  # type: ignore[union-attr]
        return
    await _finish(message, state, session, reminders, telegram_id, via_message=via_message)


async def _finish(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    reminders: RemindersService,
    telegram_id: int,
    *,
    via_message: bool = False,
) -> None:
    """Финал онбординга: слоты включаются, `onboarded=1`, главное меню (§6.1).

    Сначала commit настроек, затем job'ы (§7.2: ничего человеческого внутри
    транзакции); `ensure_slot` идемпотентен.
    """
    data = await state.get_data()
    user = await measurements_service.get_or_create_user(session, telegram_id)
    user.timezone = data.get("onb_tz") or user.timezone
    target = tuple(data.get("onb_target") or DEFAULT_TARGET)
    user.target_systolic, user.target_diastolic = target
    user.onboarded = True
    slots = [s for s in keyboards.SLOT_ORDER if s in data.get("onb_slots", ())]
    times = data.get("onb_times", {})
    await session.commit()
    for slot in slots:
        await reminders.ensure_slot(
            user.id, slot, times.get(slot) or DEFAULT_SLOT_TIMES[slot], user.timezone
        )
    await state.clear()
    if via_message:
        await message.answer(texts.ONB_DONE, reply_markup=keyboards.main_menu())
    else:
        await message.edit_text(texts.ONB_DONE, reply_markup=keyboards.main_menu())  # type: ignore[union-attr]
