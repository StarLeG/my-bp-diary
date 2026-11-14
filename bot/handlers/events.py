"""«➕ Добавить запись» — типы записей дневника (SPEC.md, §6.14; 0.6.0).

Семь типов из меню (R2 = А): давление (`add:bp` — калькулятор §6.3), приём
лекарства (`add:med` — фасад над `medication_logs`, §6.7), прогулка, питание,
стресс/самочувствие, сон, заметка. События пишутся в `diary_events` (§4.18)
через `bot.services.events`; правка/удаление — по id с проверкой владельца
(C3 = А).

Ключевые правила:
- «Питание» — один экран (K1 = А): метки + radio времени; «Готово» без меток —
  тост «Отметьте, что выбрали».
- «Сон» — без времени, одна оценка на сутки (повтор обновляет, C23).
- Шаг времени — «Сейчас» / «Другое» (`ЧЧ:ММ`, последние 24 часа, C13); в
  правке — «Оставить {момент}» / «Другое» (V9). «Отмена» на вводе — на
  предыдущий шаг с сохранением значений (V8).
- Правка: финал — `ev:save:{id}` для walk/stress/note/food; у `sleep` кнопки
  оценки остаются, обработчик делает UPDATE по флагу правки (V10).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import exists, insert, literal, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot import keyboards, schemas, texts
from bot.models import DiaryEvent, MedicationLog, User
from bot.services import events as events_service
from bot.services import measurements as measurements_service
from bot.services import medications as medications_service
from bot.services.reminders import RemindersService

router = Router(name="events")

#: Тип черновика события (walk/food/stress/sleep/note).
EV_TYPE = "ev_type"
#: Данные типа: длительность, метки, вариант или оценка.
EV_DATA = "ev_data"
#: Текст события (у `note` — весь текст, у `stress` — в заметку).
EV_NOTE = "ev_note"
#: Режим времени: `now` / `keep` (правка) / `other`.
EV_TIME_MODE = "ev_time_mode"
#: Выбранный момент события (ISO 8601, UTC).
EV_MOMENT = "ev_moment"
#: Id правки события (канон `m_edit_id`).
EV_EDIT_ID = "ev_edit_id"
#: Текст обязателен (вариант «Другое» у стресса).
EV_TEXT_REQUIRED = "ev_text_required"

#: Эмодзи типов событий (§6.4).
EVENT_EMOJI = {
    "food": "🥗",
    "stress": "😐",
    "sleep": "😴",
    "activity": "🚶",
    "note": "📝",
}

SLEEP_TYPE = "sleep"


class EventInput(StatesGroup):
    """FSM события: текст и время (§8); выборы — кнопками без FSM."""

    text = State()
    time = State()


# ─────────────────────────── Хелперы ───────────────────────────


async def _show_menu(message: Message, *, via_message: bool = False) -> None:
    """Экран выбора типа записи (§6.14)."""
    if via_message:
        await message.answer(texts.EVENT_MENU_ASK, reply_markup=keyboards.add_menu_kb())
    else:
        await message.edit_text(  # type: ignore[union-attr]
            texts.EVENT_MENU_ASK, reply_markup=keyboards.add_menu_kb()
        )


def _kind_line(event: DiaryEvent) -> str:
    """Строка типа для карточки: «Питание: солёное, кофе» и т.п. (§6.4, M5)."""
    payload = json.loads(event.payload) if event.payload else {}
    if event.type == "food":
        marks = ", ".join(texts.FACTOR_LABELS[m] for m in payload.get("marks", []))
        return f"Питание: {marks}"
    if event.type == "stress":
        variant = payload.get("variant", "stress")
        return f"Стресс: {texts.STRESS_VARIANT_LABELS.get(variant, variant)}"
    if event.type == SLEEP_TYPE:
        rating = payload.get("rating", "normal")
        return f"Сон: {texts.SLEEP_RATING_LABELS.get(rating, rating)}"
    if event.type == "activity":
        return f"Прогулка: {payload.get('duration_min', 0)} минут"
    return event.note or "Заметка"


def _moment_str(event: DiaryEvent, user: User) -> Optional[str]:
    """Момент события «ЧЧ:ММ» в таймзоне пользователя; у `sleep` — нет (C22)."""
    if event.type == SLEEP_TYPE:
        return None
    return event.moment.astimezone(ZoneInfo(user.timezone)).strftime("%H:%M")


def _card_screen(
    event: DiaryEvent, user: User
) -> tuple[str, InlineKeyboardMarkup]:
    """Карточка события: текст по типу + `note` отдельной строкой (§6.4, C3, M5)."""
    text = texts.fmt_event_card(
        EVENT_EMOJI.get(event.type, "📝"),
        _kind_line(event),
        _moment_str(event, user),
        event.note,
    )
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text=texts.BTN_EDIT, callback_data=f"ev:edit:{event.id}")
    )
    builder.row(
        InlineKeyboardButton(
            text=texts.BTN_DELETE_EVENT, callback_data=f"ev:del:{event.id}"
        )
    )
    builder.row(
        InlineKeyboardButton(text=texts.BTN_BACK, callback_data="ev:back")
    )
    return text, builder.as_markup()


async def _finish_event(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    user: User,
    type_: str,
    payload: Optional[str],
    note: Optional[str],
    moment: datetime,
    *,
    via_message: bool = False,
) -> None:
    """Сохранить событие (создание или правка) и показать экран действий (C3)."""
    data = await state.get_data()
    edit_id = data.get(EV_EDIT_ID)
    if edit_id is not None:
        event = await events_service.update_event(
            session, user, edit_id, moment=moment, note=note, payload=payload
        )
        if event is None:
            await state.clear()
            await message.edit_text(  # type: ignore[union-attr]
                texts.NOT_YOUR_RECORD, reply_markup=keyboards.back_to_menu()
            )
            return
        updated = True
    else:
        event = await events_service.create_event(
            session, user, type_, moment, note=note, payload=payload
        )
        updated = False
    await session.commit()
    await state.clear()
    await _show_saved(message, event, user, updated=updated, via_message=via_message)


async def _show_saved(
    message: Message,
    event: DiaryEvent,
    user: User,
    *,
    updated: bool,
    via_message: bool = False,
) -> None:
    """«✅ Записано» / «✅ Обновлено» + «✏️ Изменить» / «🗑 Удалить» / «➕ Ещё»."""
    text = texts.EVENT_UPDATED if updated else texts.FACTOR_DONE
    markup = keyboards.event_saved_kb(event.id, event.type)
    if via_message:
        await message.answer(text, reply_markup=markup)
    else:
        await message.edit_text(text, reply_markup=markup)  # type: ignore[union-attr]


def _time_screen_markup(
    data: dict, *, large: bool = False
) -> InlineKeyboardMarkup:
    """Шаг времени: «Сейчас» / «Оставить {момент}» (правка, V9) / «Другое»."""
    mode = data.get(EV_TIME_MODE, "now")
    edit_id = data.get(EV_EDIT_ID)
    builder = InlineKeyboardBuilder()
    if edit_id is not None:
        keep_moment = data.get(EV_MOMENT)
        label = texts.EVENT_TIME_KEEP.format(
            moment=(
                datetime.fromisoformat(keep_moment).strftime("%d.%m.%Y, %H:%M")
                if keep_moment
                else "—"
            )
        )
        rows = [(label, f"ev:save:{edit_id}"), (texts.EVENT_TIME_OTHER, "add:time:other")]
    else:
        rows = [
            (texts.EVENT_TIME_NOW, "add:time:now"),
            (texts.EVENT_TIME_OTHER, "add:time:other"),
        ]
    keyboards._layout_rows(builder, rows, large=large)  # noqa: SLF001 — канон §11
    builder.row(InlineKeyboardButton(text=texts.BTN_CANCEL, callback_data="add:back"))
    return builder.as_markup()


async def _start_time_step(
    message: Message, state: FSMContext, *, via_message: bool = False, large: bool = False
) -> None:
    """Шаг времени события (§6.14, C22)."""
    data = await state.get_data()
    await state.set_state(EventInput.time)
    markup = _time_screen_markup(data, large=large)
    if via_message:
        await message.answer(texts.EVENT_TIME_ASK, reply_markup=markup)
    else:
        await message.edit_text(texts.EVENT_TIME_ASK, reply_markup=markup)  # type: ignore[union-attr]


async def _start_text_step(
    message: Message,
    state: FSMContext,
    *,
    required: bool,
    prompt: str,
    via_message: bool = False,
    large: bool = False,
) -> None:
    """Шаг текста события: «Пропустить» — кроме обязательного «Другое» (M6)."""
    await state.set_state(EventInput.text)
    await state.update_data(**{EV_TEXT_REQUIRED: required})
    markup = keyboards.event_text_kb(required=required, large=large)
    if via_message:
        await message.answer(prompt, reply_markup=markup)
    else:
        await message.edit_text(prompt, reply_markup=markup)  # type: ignore[union-attr]


# ─────────────────────────── Меню типов ───────────────────────────


@router.callback_query(F.data == "add:menu")
async def cb_add_menu(callback: CallbackQuery, state: FSMContext) -> None:
    """«➕ Добавить запись» — экран выбора типа (§6.14); выход — «В меню» (M5)."""
    await state.clear()
    await _show_menu(callback.message)  # type: ignore[arg-type]
    await callback.answer()


@router.callback_query(F.data == "add:cancel")
async def cb_add_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    """«Отмена» на первом шаге типа — в «➕ Добавить запись» (V5)."""
    await state.clear()
    await _show_menu(callback.message)  # type: ignore[arg-type]
    await callback.answer()


@router.callback_query(F.data == "add:bp")
async def cb_add_bp(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    """«Давление и пульс» — сразу калькулятор §6.3 (без архива и правки)."""
    from bot.handlers.measure import M_BUF, M_FIELD, MeasureInput, _show_calc

    user = await measurements_service.get_or_create_user(session, callback.from_user.id)
    await state.clear()
    await state.set_state(MeasureInput.active)
    await state.update_data(**{M_BUF: "", M_FIELD: "sys"})
    await _show_calc(callback.message, state, large=user.ui_large)  # type: ignore[arg-type]
    await callback.answer()


@router.callback_query(F.data == "add:med")
async def cb_add_med(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession
) -> None:
    """«Приём лекарства» — фасад над `medication_logs` (§6.7, B3): выбор приёма.

    Пустой список лекарств — «Сначала добавьте лекарства в настройках» +
    «Добавить» (C10).
    """
    user = await measurements_service.get_or_create_user(session, callback.from_user.id)
    meds = await medications_service.list_medications(session, user)
    await state.clear()
    if not meds:
        await callback.message.edit_text(  # type: ignore[union-attr]
            texts.MEDS_NONE_HINT, reply_markup=keyboards.meds_none_kb()
        )
        await callback.answer()
        return
    builder = InlineKeyboardBuilder()
    for med in meds:
        builder.row(
            InlineKeyboardButton(
                text=texts.fmt_intake_item(med.name, med.dosage, med.slot),
                callback_data=f"add:med:pick:{med.id}",
            )
        )
    builder.row(InlineKeyboardButton(text=texts.BTN_CANCEL, callback_data="add:cancel"))
    await callback.message.edit_text(  # type: ignore[union-attr]
        texts.MED_PICK_ASK, reply_markup=builder.as_markup()
    )
    await callback.answer()


@router.callback_query(F.data.startswith("add:med:time:"))
async def cb_add_med_time(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    reminders: "RemindersService",
) -> None:
    """Время приёма: `now` — текущий момент, `other` — ввод `ЧЧ:ММ` (C4)."""
    mode = callback.data.split(":", 3)[3]  # type: ignore[union-attr]
    if mode == "now":
        user = await measurements_service.get_or_create_user(
            session, callback.from_user.id
        )
        await _save_med_intake(
            callback.message,  # type: ignore[arg-type]
            state,
            session,
            user,
            datetime.now(timezone.utc),
            reminders=reminders,
        )
        await callback.answer()
        return
    if mode != "other":
        await callback.answer(texts.STALE_MESSAGE)
        return
    await state.set_state(EventInput.time)
    await callback.message.edit_text(  # type: ignore[union-attr]
        texts.EVENT_TIME_ASK,
        reply_markup=keyboards.event_text_kb(required=True),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("add:med:pick:"))
async def cb_add_med_pick(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession
) -> None:
    """Выбор приёма — статус «Принял» / «Пропустил» / «Частично» (§6.7, B3)."""
    user = await measurements_service.get_or_create_user(session, callback.from_user.id)
    raw = callback.data.split(":", 3)[3]  # type: ignore[union-attr]
    if not raw.isdigit():
        await callback.answer(texts.STALE_MESSAGE)
        return
    med_id = int(raw)
    meds = await medications_service.list_medications(session, user)
    med = next((m for m in meds if m.id == med_id), None)
    if med is None:
        await callback.answer(texts.NOT_YOUR_RECORD)
        return
    await state.update_data(**{EV_DATA: med_id})
    builder = InlineKeyboardBuilder()
    for status, label in (
        ("taken", "Принял"),
        ("skipped", "Пропустил"),
        ("partial", "Частично"),
    ):
        builder.row(
            InlineKeyboardButton(
                text=label, callback_data=f"add:med:status:{status}:{med_id}"
            )
        )
    builder.row(InlineKeyboardButton(text=texts.BTN_CANCEL, callback_data="add:cancel"))
    await callback.message.edit_text(  # type: ignore[union-attr]
        f"{texts.fmt_intake_item(med.name, med.dosage, med.slot)} — как прошло?",
        reply_markup=builder.as_markup(),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("add:med:status:"))
async def cb_add_med_status(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession
) -> None:
    """Статус приёма → время («Сейчас» / «Другое», C4) → запись `medication_logs`."""
    parts = (callback.data or "").split(":")
    status, raw_id = parts[3], parts[4]
    if status not in ("taken", "skipped", "partial") or not raw_id.isdigit():
        await callback.answer(texts.STALE_MESSAGE)
        return
    await state.update_data(**{EV_DATA: int(raw_id), EV_TYPE: status})
    await state.set_state(EventInput.time)
    await state.update_data(**{EV_TIME_MODE: "now"})
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text=texts.EVENT_TIME_NOW, callback_data="add:med:time:now")
    )
    builder.row(
        InlineKeyboardButton(
            text=texts.EVENT_TIME_OTHER, callback_data="add:med:time:other"
        )
    )
    builder.row(InlineKeyboardButton(text=texts.BTN_CANCEL, callback_data="add:cancel"))
    await callback.message.edit_text(  # type: ignore[union-attr]
        texts.EVENT_TIME_ASK, reply_markup=builder.as_markup()
    )
    await callback.answer()


async def _save_med_intake(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    user: User,
    moment: datetime,
    *,
    reminders: Optional[RemindersService] = None,
    via_message: bool = False,
) -> None:
    """Запись приёма в `medication_logs` с дедупом за сутки (§7.2, C4).

    При статусе `taken` и включённом «Повторе после лекарства» — повторный
    замер: один активный на пользователя (`measurement_id = NULL`, C2/C18).
    """
    data = await state.get_data()
    med_id, status = data.get(EV_DATA), data.get(EV_TYPE)
    if not isinstance(med_id, int) or status not in ("taken", "skipped", "partial"):
        await state.clear()
        await _show_menu(message, via_message=via_message)
        return
    meds = await medications_service.list_medications(session, user)
    med = next((m for m in meds if m.id == med_id), None)
    if med is None:
        await state.clear()
        await message.edit_text(  # type: ignore[union-attr]
            texts.NOT_YOUR_RECORD, reply_markup=keyboards.back_to_menu()
        )
        return
    day_start, day_end = measurements_service.day_bounds_utc(
        user.timezone, moment.astimezone(ZoneInfo(user.timezone)).date()
    )
    duplicate = exists(
        select(literal(1)).where(
            MedicationLog.user_id == user.id,
            MedicationLog.medication_id == med_id,
            MedicationLog.slot == med.slot,
            MedicationLog.timestamp >= day_start,
            MedicationLog.timestamp < day_end,
        )
    )
    stmt = insert(MedicationLog).from_select(
        ["user_id", "medication_id", "timestamp", "status", "slot"],
        select(
            literal(user.id),
            literal(med_id),
            literal(moment),
            literal(status),
            literal(med.slot),
        ).where(~duplicate),
    )
    result = await session.execute(stmt)
    await session.commit()
    await state.clear()
    text = f"✅ {texts.fmt_intake_item(med.name, med.dosage, med.slot)}"
    if via_message:
        await message.answer(text, reply_markup=keyboards.saved_kb())
    else:
        await message.edit_text(text, reply_markup=keyboards.saved_kb())  # type: ignore[union-attr]
    delay = user.med_remeasure_delay
    if (
        delay
        and status == "taken"
        and result.rowcount
        and reminders is not None
    ):
        # повторный замер без измерения: один активный на пользователя (C2/C18)
        await reminders.ensure_remeasure(user.id, None, delay)


# ─────────────────────────── Прогулка ───────────────────────────


@router.callback_query(F.data == "add:walk")
async def cb_add_walk(callback: CallbackQuery, state: FSMContext) -> None:
    """«Прогулка / активность» — шаг длительности (§6.14, R17 = Б)."""
    await state.clear()
    await state.update_data(**{EV_TYPE: "activity", EV_TIME_MODE: "now"})
    await callback.message.edit_text(  # type: ignore[union-attr]
        texts.WALK_ASK, reply_markup=keyboards.walk_kb()
    )
    await callback.answer()


@router.callback_query(F.data.startswith("add:walk:"))
async def cb_add_walk_pick(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession
) -> None:
    """Длительность выбрана — шаг времени («Сейчас» / «Другое»)."""
    raw = callback.data.split(":", 2)[2]  # type: ignore[union-attr]
    if raw not in ("10", "20", "30", "60"):
        await callback.answer(texts.STALE_MESSAGE)
        return
    await state.update_data(**{EV_DATA: int(raw)})
    user = await measurements_service.get_or_create_user(session, callback.from_user.id)
    await _start_time_step(callback.message, state, large=user.ui_large)  # type: ignore[arg-type]
    await callback.answer()


# ─────────────────────────── Питание (один экран, K1) ───────────────────────────


@router.callback_query(F.data == "add:food")
async def cb_add_food(callback: CallbackQuery, state: FSMContext) -> None:
    """«Питание» — один экран: метки + radio времени (§6.14, K1 = А)."""
    await state.clear()
    await state.update_data(
        **{EV_TYPE: "food", EV_DATA: [], EV_TIME_MODE: "now"}
    )
    await callback.message.edit_text(  # type: ignore[union-attr]
        texts.EVENT_TYPE_LABELS["food"] + "?",
        reply_markup=keyboards.food_kb(set()),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("add:food:pick:"))
async def cb_add_food_pick(
    callback: CallbackQuery, state: FSMContext
) -> None:
    """Переключить метку питания (чекбокс — каждое нажатие, exempt от debounce)."""
    mark = callback.data.split(":", 3)[3]  # type: ignore[union-attr]
    if mark not in ("salt", "coffee", "alcohol"):
        await callback.answer(texts.STALE_MESSAGE)
        return
    data = await state.get_data()
    if data.get(EV_TYPE) != "food":
        await _show_menu(callback.message)  # type: ignore[arg-type]
        await callback.answer()
        return
    checked = set(data.get(EV_DATA, []))
    if mark in checked:
        checked.discard(mark)
    else:
        checked.add(mark)
    await state.update_data(**{EV_DATA: sorted(checked)})
    await callback.message.edit_text(  # type: ignore[union-attr]
        texts.EVENT_TYPE_LABELS["food"] + "?",
        reply_markup=keyboards.food_kb(checked, data.get(EV_TIME_MODE, "now")),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("add:food:time:"))
async def cb_add_food_time(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession
) -> None:
    """Radio времени питания: `now` — выбрать «Сейчас», `other` — ввод `ЧЧ:ММ`
    без сохранения (K1); «Отмена» ввода — назад с метками и «Сейчас» (M2)."""
    mode = callback.data.split(":", 3)[3]  # type: ignore[union-attr]
    data = await state.get_data()
    if data.get(EV_TYPE) != "food":
        await _show_menu(callback.message)  # type: ignore[arg-type]
        await callback.answer()
        return
    if mode == "now":
        await state.update_data(**{EV_TIME_MODE: "now", EV_MOMENT: None})
        await callback.message.edit_text(  # type: ignore[union-attr]
            texts.EVENT_TYPE_LABELS["food"] + "?",
            reply_markup=keyboards.food_kb(set(data.get(EV_DATA, [])), "now"),
        )
        await callback.answer()
        return
    if mode != "other":
        await callback.answer(texts.STALE_MESSAGE)
        return
    await state.update_data(**{EV_TIME_MODE: "other"})
    await state.set_state(EventInput.time)
    user = await measurements_service.get_or_create_user(session, callback.from_user.id)
    await callback.message.edit_text(  # type: ignore[union-attr]
        texts.EVENT_TIME_ASK,
        reply_markup=keyboards.event_text_kb(required=True, large=user.ui_large),
    )
    await callback.answer()


@router.callback_query(F.data == "add:food:done")
async def cb_add_food_done(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession
) -> None:
    """«Готово»: сохранить «Питание» с выбранным временем (§6.14, K1).

    Без меток — тост «Отметьте, что выбрали»; выбрано «Другое время» без
    введённого — тост «Введите время» (V6).
    """
    user = await measurements_service.get_or_create_user(session, callback.from_user.id)
    data = await state.get_data()
    if data.get(EV_TYPE) != "food":
        await _show_menu(callback.message)  # type: ignore[arg-type]
        await callback.answer()
        return
    marks = set(data.get(EV_DATA, []))
    if not marks:
        await callback.answer(texts.FACTOR_EMPTY)  # экран не меняется
        return
    moment = await _resolve_moment(state, data, user, callback)
    if moment is None:
        return
    payload = events_service.event_payload("food", marks=marks)
    await _finish_event(
        callback.message, state, session, user, "food", payload, None, moment  # type: ignore[arg-type]
    )
    await callback.answer()


# ─────────────────── Стресс / самочувствие ───────────────────


@router.callback_query(F.data == "add:stress")
async def cb_add_stress(callback: CallbackQuery, state: FSMContext) -> None:
    """«Стресс / самочувствие» — вариант «Что чувствуете?» (§6.14, I7)."""
    await state.clear()
    await state.update_data(**{EV_TYPE: "stress", EV_TIME_MODE: "now"})
    await callback.message.edit_text(  # type: ignore[union-attr]
        texts.STRESS_ASK, reply_markup=keyboards.stress_kb()
    )
    await callback.answer()


@router.callback_query(F.data.startswith("add:stress:"))
async def cb_add_stress_pick(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession
) -> None:
    """Вариант выбран → шаг текста (для «Другое» — обязательный, без «Пропустить»)."""
    variant = callback.data.split(":", 2)[2]  # type: ignore[union-attr]
    if variant not in texts.STRESS_VARIANT_LABELS:
        await callback.answer(texts.STALE_MESSAGE)
        return
    await state.update_data(**{EV_DATA: variant})
    user = await measurements_service.get_or_create_user(session, callback.from_user.id)
    required = variant == "other"
    prompt = texts.EVENT_TEXT_REQUIRED if required else texts.EVENT_TEXT_PROMPT
    await _start_text_step(
        callback.message,  # type: ignore[arg-type]
        state,
        required=required,
        prompt=prompt,
        large=user.ui_large,
    )
    await callback.answer()


# ─────────────────────────── Сон ───────────────────────────


@router.callback_query(F.data == "add:sleep")
async def cb_add_sleep(callback: CallbackQuery, state: FSMContext) -> None:
    """«Сон» — оценка без времени (§6.14, R20 = А)."""
    await state.clear()
    await state.update_data(**{EV_TYPE: SLEEP_TYPE})
    await callback.message.edit_text(  # type: ignore[union-attr]
        texts.SLEEP_ASK, reply_markup=keyboards.sleep_kb()
    )
    await callback.answer()


@router.callback_query(F.data.startswith("add:sleep:"))
async def cb_add_sleep_pick(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession
) -> None:
    """Оценка сна: одна на локальные сутки, повтор обновляет (C23); в правке —
    UPDATE по флагу `ev_edit_id`, момент не меняется (V10)."""
    rating = callback.data.split(":", 2)[2]  # type: ignore[union-attr]
    if rating not in texts.SLEEP_RATING_LABELS:
        await callback.answer(texts.STALE_MESSAGE)
        return
    user = await measurements_service.get_or_create_user(session, callback.from_user.id)
    data = await state.get_data()
    payload = events_service.event_payload("sleep", rating=rating)
    edit_id = data.get(EV_EDIT_ID)
    if edit_id is not None:
        moment = datetime.fromisoformat(data[EV_MOMENT])
        await _finish_event(
            callback.message, state, session, user, SLEEP_TYPE, payload, None, moment  # type: ignore[arg-type]
        )
        await callback.answer()
        return
    now = datetime.now(timezone.utc)
    event = await events_service.upsert_sleep(
        session,
        user,
        now.astimezone(ZoneInfo(user.timezone)).date(),
        rating,
        now,
    )
    await session.commit()
    await state.clear()
    await _show_saved(callback.message, event, user, updated=False)  # type: ignore[arg-type]
    await callback.answer()


# ─────────────────────────── Заметка ───────────────────────────


@router.callback_query(F.data == "add:note")
async def cb_add_note(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession
) -> None:
    """«Заметка / другое» — свободный текст ≤ 500 (§6.14; пустой — ошибка §9)."""
    await state.clear()
    await state.update_data(**{EV_TYPE: "note", EV_TIME_MODE: "now"})
    user = await measurements_service.get_or_create_user(session, callback.from_user.id)
    await _start_text_step(
        callback.message,  # type: ignore[arg-type]
        state,
        required=True,
        prompt=texts.NOTE_ASK,
        large=user.ui_large,
    )
    await callback.answer()


# ─────────────────── Текст и время (общие шаги) ───────────────────


@router.callback_query(F.data == "add:text:skip")
async def cb_add_text_skip(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession
) -> None:
    """«Пропустить» текст (M6) — дальше по флоу: шаг времени."""
    await state.update_data(**{EV_NOTE: None})
    user = await measurements_service.get_or_create_user(session, callback.from_user.id)
    await _start_time_step(callback.message, state, large=user.ui_large)  # type: ignore[arg-type]
    await callback.answer()


@router.message(EventInput.text)
async def msg_event_text(message: Message, state: FSMContext, session: AsyncSession) -> None:
    """Текст события: у `note` обязателен (§9), у `stress` — по варианту (§6.14)."""
    user = await measurements_service.get_or_create_user(
        session, message.from_user.id  # type: ignore[union-attr]
    )
    data = await state.get_data()
    try:
        text = schemas.validate_note(message.text or "")
    except ValueError as exc:
        if data.get(EV_TYPE) == "note":
            await message.answer(str(exc))
            return
        if data.get(EV_TEXT_REQUIRED):
            await message.answer(texts.EVENT_TEXT_REQUIRED)  # «Другое» без текста
            return
        text = None  # необязательный текст невалиден — как «Пропустить»
    await state.update_data(**{EV_NOTE: text})
    await _start_time_step(message, state, via_message=True, large=user.ui_large)


@router.callback_query(F.data == "add:time:now")
async def cb_add_time_now(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession
) -> None:
    """«Сейчас» — момент события = текущий; сохранение (§6.14)."""
    user = await measurements_service.get_or_create_user(session, callback.from_user.id)
    data = await state.get_data()
    moment = datetime.now(timezone.utc)
    await state.update_data(**{EV_MOMENT: moment.isoformat()})
    await _finish_from_draft(
        callback.message, state, session, user, moment, callback=callback  # type: ignore[arg-type]
    )
    await callback.answer()
    return


@router.callback_query(F.data == "add:time:other")
async def cb_add_time_other(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession
) -> None:
    """«Другое время» — FSM-ввод `ЧЧ:ММ` без сохранения (K1)."""
    await state.set_state(EventInput.time)
    user = await measurements_service.get_or_create_user(session, callback.from_user.id)
    await callback.message.edit_text(  # type: ignore[union-attr]
        texts.EVENT_TIME_ASK,
        reply_markup=keyboards.event_text_kb(required=True, large=user.ui_large),
    )
    await callback.answer()


@router.callback_query(F.data == "add:back")
async def cb_add_back(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession
) -> None:
    """«Отмена» FSM-ввода — на предыдущий шаг с сохранением значений (V8, M2)."""
    data = await state.get_data()
    type_ = data.get(EV_TYPE)
    user = await measurements_service.get_or_create_user(session, callback.from_user.id)
    await state.set_state(None)
    if type_ == "food":
        await state.update_data(**{EV_TIME_MODE: "now", EV_MOMENT: None})
        await callback.message.edit_text(  # type: ignore[union-attr]
            texts.EVENT_TYPE_LABELS["food"] + "?",
            reply_markup=keyboards.food_kb(set(data.get(EV_DATA, [])), "now"),
        )
    elif type_ == "activity":
        await _start_time_step(callback.message, state, large=user.ui_large)  # type: ignore[arg-type]
        return
    elif type_ == "stress":
        required = data.get(EV_TEXT_REQUIRED, False)
        prompt = texts.EVENT_TEXT_REQUIRED if required else texts.EVENT_TEXT_PROMPT
        await _start_text_step(
            callback.message,  # type: ignore[arg-type]
            state,
            required=required,
            prompt=prompt,
            large=user.ui_large,
        )
        return
    elif type_ == "note":
        await _start_text_step(
            callback.message,  # type: ignore[arg-type]
            state,
            required=True,
            prompt=texts.NOTE_ASK,
            large=user.ui_large,
        )
        return
    else:
        await _show_menu(callback.message)  # type: ignore[arg-type]
    await callback.answer()


@router.message(EventInput.time)
async def msg_event_time(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    reminders: RemindersService,
) -> None:
    """`ЧЧ:ММ` времени события: последние 24 часа (C13, I4); будущее/старое — ошибка."""
    user = await measurements_service.get_or_create_user(
        session, message.from_user.id  # type: ignore[union-attr]
    )
    try:
        moment_time = schemas.validate_time(message.text or "")
        moment = schemas.resolve_event_moment(moment_time, user.timezone)
    except ValueError as exc:
        await message.answer(str(exc))
        return
    data = await state.get_data()
    if data.get(EV_TYPE) in ("taken", "skipped", "partial"):
        await _save_med_intake(
            message, state, session, user, moment, reminders=reminders, via_message=True
        )
        return
    await state.update_data(**{EV_MOMENT: moment.isoformat(), EV_TIME_MODE: "other"})
    await _finish_from_draft(message, state, session, user, moment, via_message=True)


async def _resolve_moment(
    state: FSMContext,
    data: dict,
    user: User,
    callback: CallbackQuery,
) -> Optional[datetime]:
    """Момент события по radio времени; «Другое» без ввода — тост «Введите время» (V6)."""
    mode = data.get(EV_TIME_MODE, "now")
    if data.get(EV_MOMENT):
        return datetime.fromisoformat(data[EV_MOMENT])
    if mode == "other":
        await callback.answer(texts.EVENT_TIME_NEEDED)  # экран не меняется
        return None
    return datetime.now(timezone.utc)


async def _finish_from_draft(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    user: User,
    moment: datetime,
    *,
    via_message: bool = False,
    callback: Optional[CallbackQuery] = None,
) -> None:
    """Собрать событие из черновика и сохранить (создание или правка)."""
    data = await state.get_data()
    type_ = data.get(EV_TYPE)
    if type_ not in ("activity", "food", "stress", "note", SLEEP_TYPE):
        await state.clear()
        await _show_menu(message, via_message=via_message)
        return
    payload: Optional[str] = None
    note = data.get(EV_NOTE)
    if type_ == "activity":
        payload = events_service.event_payload("activity", duration_min=data.get(EV_DATA))
    elif type_ == "food":
        payload = events_service.event_payload("food", marks=set(data.get(EV_DATA, [])))
    elif type_ == "stress":
        payload = events_service.event_payload("stress", variant=data.get(EV_DATA))
    await _finish_event(
        message, state, session, user, type_, payload, note, moment, via_message=via_message
    )


@router.callback_query(F.data.startswith("ev:save:"))
async def cb_event_save(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession
) -> None:
    """Финал правки события (`ev:save:{id}`, V10): «Оставить {момент}» — сохранить как есть."""
    user = await measurements_service.get_or_create_user(session, callback.from_user.id)
    data = await state.get_data()
    raw = callback.data.split(":", 2)[2]  # type: ignore[union-attr]
    if not raw.isdigit() or data.get(EV_EDIT_ID) != int(raw):
        await callback.answer(texts.STALE_MESSAGE)
        return
    moment = datetime.fromisoformat(data[EV_MOMENT])
    await _finish_from_draft(
        callback.message, state, session, user, moment, callback=callback  # type: ignore[arg-type]
    )
    await callback.answer()


# ─────────────────── Карточка, правка, удаление (C3) ───────────────────


@router.callback_query(F.data.startswith("ev:open:"))
async def cb_event_open(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession
) -> None:
    """Карточка события: текст по типу, `note` отдельной строкой (§6.4, M5)."""
    user = await measurements_service.get_or_create_user(session, callback.from_user.id)
    raw = callback.data.split(":", 2)[2]  # type: ignore[union-attr]
    if not raw.isdigit():
        await callback.answer(texts.STALE_MESSAGE)
        return
    event = await events_service.get_event(session, user, int(raw))
    if event is None:
        await callback.answer(texts.STALE_MESSAGE)
        return
    text, markup = _card_screen(event, user)
    await callback.message.edit_text(text, reply_markup=markup)  # type: ignore[union-attr]
    await callback.answer()


@router.callback_query(F.data == "ev:back")
async def cb_event_back(callback: CallbackQuery, state: FSMContext) -> None:
    """«Назад» из карточки — в «➕ Добавить запись» (лента — шаг 8 плана)."""
    await _show_menu(callback.message)  # type: ignore[arg-type]
    await callback.answer()


@router.callback_query(F.data.startswith("ev:del:"))
async def cb_event_del(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession
) -> None:
    """«🗑 Удалить» — подтверждение (§6.14); `ev:del:{id}:ok` — удаление."""
    parts = (callback.data or "").split(":")
    raw = parts[2]
    if len(parts) == 4 and parts[3] == "ok":
        await _delete_event(callback, state, session, raw)
        return
    if not raw.isdigit():
        await callback.answer(texts.STALE_MESSAGE)
        return
    user = await measurements_service.get_or_create_user(session, callback.from_user.id)
    event = await events_service.get_event(session, user, int(raw))
    if event is None:
        await callback.answer(texts.STALE_MESSAGE)
        return
    await callback.message.edit_text(  # type: ignore[union-attr]
        texts.EVENT_DEL_CONFIRM, reply_markup=keyboards.event_del_kb(event.id)
    )
    await callback.answer()


async def _delete_event(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession, raw: str
) -> None:
    """Удалить событие по id; чужое/удалённое — тост (§7.1)."""
    if not raw.isdigit():
        await callback.answer(texts.STALE_MESSAGE)
        return
    user = await measurements_service.get_or_create_user(session, callback.from_user.id)
    deleted = await events_service.delete_event(session, user, int(raw))
    if not deleted:
        await callback.answer(texts.STALE_MESSAGE)
        return
    await session.commit()
    await callback.message.edit_text(  # type: ignore[union-attr]
        texts.EVENT_DELETED, reply_markup=keyboards.add_menu_kb()
    )
    await callback.answer()


@router.callback_query(F.data.startswith("ev:edit:"))
async def cb_event_edit(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession
) -> None:
    """«✏️ Изменить» — перезапуск флоу типа с текущими значениями (C3, V9, V10)."""
    user = await measurements_service.get_or_create_user(session, callback.from_user.id)
    raw = callback.data.split(":", 2)[2]  # type: ignore[union-attr]
    if not raw.isdigit():
        await callback.answer(texts.STALE_MESSAGE)
        return
    event = await events_service.get_event(session, user, int(raw))
    if event is None:
        await callback.answer(texts.STALE_MESSAGE)
        return
    payload = json.loads(event.payload) if event.payload else {}
    await state.clear()
    await state.update_data(
        **{
            EV_TYPE: event.type,
            EV_EDIT_ID: event.id,
            EV_MOMENT: event.moment.isoformat(),
            EV_NOTE: event.note,
            EV_TIME_MODE: "keep",
        }
    )
    if event.type == "activity":
        await state.update_data(**{EV_DATA: payload.get("duration_min")})
        await _start_time_step(callback.message, state, large=user.ui_large)  # type: ignore[arg-type]
    elif event.type == "food":
        await state.update_data(**{EV_DATA: sorted(payload.get("marks", []))})
        await callback.message.edit_text(  # type: ignore[union-attr]
            texts.EVENT_TYPE_LABELS["food"] + "?",
            reply_markup=keyboards.food_kb(
                set(payload.get("marks", [])), "keep"
            ),
        )
    elif event.type == "stress":
        await state.update_data(**{EV_DATA: payload.get("variant", "stress")})
        required = payload.get("variant") == "other"
        prompt = texts.EVENT_TEXT_REQUIRED if required else texts.EVENT_TEXT_PROMPT
        await _start_text_step(
            callback.message,  # type: ignore[arg-type]
            state,
            required=required,
            prompt=prompt,
            large=user.ui_large,
        )
    elif event.type == SLEEP_TYPE:
        await state.update_data(**{EV_DATA: payload.get("rating")})
        await callback.message.edit_text(  # type: ignore[union-attr]
            texts.SLEEP_ASK, reply_markup=keyboards.sleep_kb()
        )
    else:  # note
        await _start_text_step(
            callback.message,  # type: ignore[arg-type]
            state,
            required=True,
            prompt=texts.NOTE_ASK,
            large=user.ui_large,
        )
    await callback.answer()
