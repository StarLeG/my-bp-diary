"""Хендлеры записи давления (SPEC.md, §6.3).

Ввод значений — клавиатура-калькулятор: цифры, «C» (очистить), «⌫» (стереть
последнюю цифру), «Готово»; на шаге пульса дополнительно «Пропустить». Буфер
набора живёт в FSM (`M_BUF`, не более 3 цифр, ведущие нули схлопываются);
текущий набор показан в тексте экрана. Число, написанное в чат, — сразу
подтверждённое значение и переход к следующему шагу; не-число или значение
вне диапазона — короткая ошибка и повтор ввода.

Сохранение — сразу после пульса: INSERT новой записи или UPDATE правки
(`M_EDIT_ID`, «✅ Записано» / «✅ Обновлено»); далее — кнопки опциональных
шагов: заметка, «Приняли лекарство?» (Да/Нет), самочувствие. «Да» планирует
повторный замер (`RemindersService.ensure_remeasure`, кнопка «Не надо»,
идемпотентно) и при настроенных лекарствах показывает мультивыбор «Какие
лекарства приняли?» (`m:med:pick`/`m:med:done` → `log_manual_intake`,
идемпотентно за локальные сутки); после первой «Да» без лекарств — мягкая
подсказка добавить их (один раз). Кнопки редактируют текущее сообщение;
ответы на свободный ввод — новыми сообщениями. Черновик живёт в FSM
(`M_SYS`/`M_DIA`/`M_PULSE`/`M_BUF`/`M_MED_CHECKED`), id сохранённой записи —
в `M_ID`, id правки — в `M_EDIT_ID`: чужие id через callback_data сюда
не попадают без проверки владельца.

Архивные записи (SPEC.md, §6.10) — тот же числовой поток, но с явным
моментом измерения в `M_ARCH_TS` (канон `m_edit_id`): дата и время
собираются шагами «Когда?» / «Во сколько?» (`ArchiveInput`) до калькулятора,
INSERT идёт с указанным `timestamp`. Для архива повторный замер после
лекарства и `medication_logs` не планируются, строка «обсудить с врачом» не
показывается; сводка — кнопки `m:arch:*` с «➕ Ещё запись».
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

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
#: Буфер набора калькулятора (строка ≤ 3 цифр, §6.3).
M_BUF = "m_buf"
#: Отмеченные приёмы мультивыбора «Какие лекарства приняли?» (список id).
M_MED_CHECKED = "m_med_checked"
#: Id приёмов, принятых за сегодня, — для пометки «· сегодня ✓» при перерисовке (§6.3).
M_MED_TAKEN_TODAY = "m_med_taken_today"
M_ID = "m_id"
#: Id редактируемой записи («последняя» фиксируется в момент входа в правку, §6.3).
M_EDIT_ID = "m_edit_id"
#: Момент архивного измерения (ISO 8601, UTC); режим архива — по наличию ключа
#: (канон `m_edit_id`, §8): финальное сохранение делает INSERT с этим `timestamp`.
M_ARCH_TS = "m_arch_ts"
#: Момент архивной записи для сводки («12.03.2025, 08:00», §6.10).
M_ARCH_LABEL = "m_arch_label"
#: Дата шага «Когда?» до выбора времени (ISO, §6.10).
A_DATE = "arch_date"


class MeasureInput(StatesGroup):
    """FSM-состояния ввода флоу записи (§15)."""

    sys_input = State()
    dia_input = State()
    pulse_input = State()
    note_input = State()
    feeling_other = State()


class ArchiveInput(StatesGroup):
    """FSM-состояния архивного ввода (§8): дата, время, быстрая строка."""

    date = State()
    time = State()
    quick = State()


@dataclass(frozen=True)
class _Field:
    """Описание шага ввода значения (ключ черновика, диапазон, вопрос)."""

    key: str
    lo: int
    hi: int
    label: str
    question: str
    allow_skip: bool


#: Шаги калькулятора по FSM-состоянию (SPEC.md, раздел 6.3, §9).
_FIELDS: dict[str, _Field] = {
    MeasureInput.sys_input.state: _Field(
        M_SYS, 70, 260, "систолическое", texts.ASK_SYSTOLIC, False
    ),
    MeasureInput.dia_input.state: _Field(
        M_DIA, 40, 160, "диастолическое", texts.ASK_DIASTOLIC, False
    ),
    MeasureInput.pulse_input.state: _Field(
        M_PULSE, 30, 220, "пульс", texts.ASK_PULSE, True
    ),
}


def _buf_add(buf: str, digit: str) -> str:
    """Добавить цифру в буфер: ≤ 3 цифр, ведущие нули схлопываются (§6.3).

    Нажатие, переполняющее буфер («260» + «5»), игнорируется.
    """
    candidate = str(int(buf + digit))
    return candidate if len(candidate) <= 3 else buf


# ─────────────────────────── Экраны ───────────────────────────


async def _send(
    message: Message, text: str, markup: InlineKeyboardMarkup, *, via_message: bool
) -> None:
    """Отправить экран: свободный ввод — новым сообщением, кнопки — редактированием."""
    if via_message:
        await message.answer(text, reply_markup=markup)
    else:
        await message.edit_text(text, reply_markup=markup)  # type: ignore[union-attr]


async def _show_keypad(
    message: Message, state: FSMContext, *, via_message: bool = False
) -> None:
    """Показать экран текущего шага с текущим буфером (§6.3)."""
    current = await state.get_state()
    field = _FIELDS[current]
    data = await state.get_data()
    buf = data.get(M_BUF, "")
    await _send(
        message,
        texts.fmt_keypad_prompt(field.question, buf),
        keyboards.keypad_kb(buf, allow_skip=field.allow_skip),
        via_message=via_message,
    )


def _saved_markup(data: dict[str, Any]) -> InlineKeyboardMarkup:
    """Кнопки сводки: архивные (`M_ARCH_TS`, §6.10) или обычные (§6.3)."""
    if data.get(M_ARCH_TS) is not None:
        return keyboards.archive_saved_kb()
    return keyboards.saved_kb()


def _summary_screen(data: dict[str, Any]) -> tuple[str, InlineKeyboardMarkup]:
    """Экран сводки: обычный режим или архив (§6.3, §6.10).

    Архив (`M_ARCH_TS`) — заголовок «✅ Записано за …» и кнопки `m:arch:*`;
    правка (`M_EDIT_ID`) — «✅ Обновлено»; у архивной правки остаются кнопки
    архива («➕ Ещё запись» и «В меню» — свои).
    """
    is_archive = data.get(M_ARCH_TS) is not None
    markup = _saved_markup(data)
    if data.get(M_EDIT_ID) is not None:
        text = texts.fmt_updated(data[M_SYS], data[M_DIA], data.get(M_PULSE))
    elif is_archive:
        text = texts.fmt_archive_saved(
            data.get(M_ARCH_LABEL, ""), data[M_SYS], data[M_DIA], data.get(M_PULSE)
        )
    else:
        text = texts.fmt_saved(data[M_SYS], data[M_DIA], data.get(M_PULSE))
    return text, markup


async def _hub(message: Message, data: dict[str, Any], *, via_message: bool = False) -> None:
    """Вернуть сводку с кнопками опциональных шагов (повторное открытие)."""
    text, markup = _summary_screen(data)
    await _send(message, text, markup, via_message=via_message)


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


async def _archive_guard(callback: CallbackQuery, state: FSMContext) -> bool:
    """Устаревшая кнопка обычной сводки при архивном черновике (§6.10).

    «Приняли лекарство?» обычной сводки планирует повторный замер и пишет
    `medication_logs` — для архива это запрещено. Если в черновике архивная
    запись (`M_ARCH_TS`), вместо действия показывается сводка архива;
    возвращает True, когда обработку надо прервать.
    """
    data = await state.get_data()
    if data.get(M_ARCH_TS) is None:
        return False
    await _hub(callback.message, data)  # type: ignore[arg-type]
    await callback.answer()
    return True


async def _confirm_value(state: FSMContext, raw: str) -> Optional[str]:
    """Проверить и сохранить значение текущего шага (§9).

    Возвращает None при успехе или текст ошибки (экран при этом не меняется).
    На шаге диастолического дополнительно проверяется пара (§9).
    """
    current = await state.get_state()
    field = _FIELDS.get(current or "")
    if field is None:
        return texts.KEYPAD_EMPTY_INPUT
    try:
        value = schemas.validate_int(raw, field.lo, field.hi, field.label)
    except ValueError as exc:
        return str(exc)
    data = await state.get_data()
    if field.key == M_DIA:
        try:
            schemas.validate_pair(data[M_SYS], value)
        except ValueError as exc:
            return str(exc)
    await state.update_data(**{field.key: value, M_BUF: ""})
    return None


async def _advance(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    telegram_id: int,
    *,
    via_message: bool = False,
) -> None:
    """После подтверждения значения: следующий шаг калькулятора или сохранение."""
    current = await state.get_state()
    if current == MeasureInput.sys_input.state:
        await state.set_state(MeasureInput.dia_input)
        await _show_keypad(message, state, via_message=via_message)
    elif current == MeasureInput.dia_input.state:
        await state.set_state(MeasureInput.pulse_input)
        await _show_keypad(message, state, via_message=via_message)
    else:  # pulse_input — сохранение сразу после пульса (§6.3)
        await state.set_state(None)
        await _save(message, state, session, telegram_id, via_message=via_message)


# ─────────────────────────── Начало и отмена ───────────────────────────


@router.callback_query(
    F.data.in_({"menu:measure", "rem:write", "rem:now"}) | F.data.startswith("rem:write:")
)
async def cb_measure_begin(
    callback: CallbackQuery,
    state: FSMContext,
    session: Optional[AsyncSession] = None,
    reminders: Optional[RemindersService] = None,
) -> None:
    """«Записать давление» / «Записать сейчас» — шаг 1 (§6.3, §6.7).

    Прошлый черновик сбрасывается. `rem:write:{slot}:{д}` (0.5.6) — ответ на
    напоминание слота: повтор «если не ответил» отменяется.
    """
    parts = (callback.data or "").split(":")
    if (
        len(parts) >= 3
        and parts[1] == "write"
        and parts[2] in keyboards.SLOT_ORDER
        and session is not None
        and reminders is not None
    ):
        user = await measurements_service.get_or_create_user(session, callback.from_user.id)
        await session.commit()
        await reminders.cancel_followup(user.id, parts[2])
    await state.clear()
    await state.set_state(MeasureInput.sys_input)
    await state.update_data(**{M_BUF: ""})
    await _show_keypad(callback.message, state)  # type: ignore[arg-type]
    await callback.answer()


@router.callback_query(F.data == "m:cancel")
async def cb_measure_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    """«Отмена» — сброс черновика и FSM, возврат в главное меню (правка не сохраняется)."""
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
    await state.set_state(MeasureInput.sys_input)
    await state.update_data(
        **{M_EDIT_ID: measurement.id, M_ID: measurement.id, M_BUF: ""}
    )
    await _show_keypad(callback.message, state)  # type: ignore[arg-type]
    await callback.answer()


# ─────────────────────────── Калькулятор ───────────────────────────


@router.callback_query(F.data.startswith("m:key:"))
async def cb_keypad(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    """Кнопки калькулятора: цифры, «C», «⌫», «Готово», «Пропустить» (§6.3).

    «Готово» с пустым буфером — «Введите число»; вне диапазона или с нарушением
    пары — ошибка валидации: экран не меняется. «Пропустить» — только шаг
    пульса (значение очищается, §6.3).
    """
    action = callback.data.split(":", 2)[2]  # type: ignore[union-attr]
    current = await state.get_state()
    field = _FIELDS.get(current or "")
    if field is None:
        await _to_menu(callback)  # устаревшие кнопки без черновика
        return
    data = await state.get_data()
    buf = data.get(M_BUF, "")
    if action.isdigit():
        buf = _buf_add(buf, action)
        await state.update_data(**{M_BUF: buf})
        await _show_keypad(callback.message, state)  # type: ignore[arg-type]
    elif action == "bksp":
        buf = buf[:-1]
        await state.update_data(**{M_BUF: buf})
        await _show_keypad(callback.message, state)  # type: ignore[arg-type]
    elif action == "clear":
        await state.update_data(**{M_BUF: ""})
        await _show_keypad(callback.message, state)  # type: ignore[arg-type]
    elif action == "skip":
        if field.key != M_PULSE:
            await _to_menu(callback)  # «Пропустить» бывает только на пульсе
            return
        await state.update_data(**{M_PULSE: None, M_BUF: ""})
        await _advance(
            callback.message,  # type: ignore[arg-type]
            state,
            session,
            callback.from_user.id,
        )
    elif action == "ok":
        if not buf:
            await callback.answer(texts.KEYPAD_EMPTY_INPUT)  # экран не меняется
            return
        error = await _confirm_value(state, buf)
        if error is not None:
            await callback.answer(error)  # экран не меняется
            return
        await _advance(
            callback.message,  # type: ignore[arg-type]
            state,
            session,
            callback.from_user.id,
        )
    else:
        await _to_menu(callback)  # неизвестное действие — устаревшие кнопки
        return
    await callback.answer()


async def _msg_value(
    message: Message, state: FSMContext, session: AsyncSession
) -> None:
    """Число из чата — сразу подтверждённое значение и переход (§6.3)."""
    error = await _confirm_value(state, message.text or "")
    if error is not None:
        await message.answer(error)  # короткая ошибка, повтор ввода
        return
    tg_id = message.from_user.id  # type: ignore[union-attr]
    await _advance(message, state, session, tg_id, via_message=True)


@router.message(MeasureInput.sys_input)
async def msg_systolic(message: Message, state: FSMContext, session: AsyncSession) -> None:
    await _msg_value(message, state, session)


@router.message(MeasureInput.dia_input)
async def msg_diastolic(message: Message, state: FSMContext, session: AsyncSession) -> None:
    await _msg_value(message, state, session)


@router.message(MeasureInput.pulse_input)
async def msg_pulse(message: Message, state: FSMContext, session: AsyncSession) -> None:
    await _msg_value(message, state, session)


async def _save(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    telegram_id: int,
    *,
    via_message: bool = False,
    user: Optional[User] = None,
) -> None:
    """Сохранить черновик: INSERT новой записи или UPDATE правки (§6.3).

    Сначала commit записи, FSM обновляется после (§7.2: ничего человеческого
    внутри транзакции); дубль за минуту — «Это давление уже записано» (только
    для новой записи); правка — UPDATE без дедупа и без смены времени записи.
    Архив (`M_ARCH_TS`) — INSERT с указанным `timestamp` (§6.10); строка
    «обсудить с врачом» для архива не показывается. `user` — уже
    загруженный пользователь (без повторного запроса).
    """
    data = await state.get_data()
    if M_SYS not in data or M_DIA not in data:
        await _send(  # устаревшие кнопки без черновика — в главное меню
            message, texts.MAIN_MENU, keyboards.main_menu(), via_message=via_message
        )
        return
    sys_v, dia_v, pulse = data[M_SYS], data[M_DIA], data.get(M_PULSE)
    if user is None:
        user = await measurements_service.get_or_create_user(session, telegram_id)
    edit_id = data.get(M_EDIT_ID)
    arch_ts = data.get(M_ARCH_TS)
    if edit_id is None:
        timestamp = datetime.fromisoformat(arch_ts) if arch_ts else None
        try:
            measurement = await measurements_service.save_measurement(
                session, user, sys_v, dia_v, pulse, timestamp=timestamp
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
    await session.commit()
    await state.update_data(**{M_ID: measurement.id, M_BUF: ""})
    text, markup = _summary_screen(data)
    if arch_ts is None and (sys_v >= HIGH_SYSTOLIC or dia_v >= HIGH_DIASTOLIC):
        text = f"{text}\n{texts.HIGH_PRESSURE_WARNING}"
    await _send(message, text, markup, via_message=via_message)


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
    await message.answer(texts.NOTE_SAVED, reply_markup=_saved_markup(data))


# ─────────────────────────── «Приняли лекарство?» ───────────────────────────


@router.callback_query(F.data == "m:med")
async def cb_med(callback: CallbackQuery, state: FSMContext) -> None:
    """«Приняли лекарство?» обычной сводки (§6.3); при архиве — сводка архива."""
    if await _archive_guard(callback, state):
        return
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

    Повтор идемпотентен по id записи; кнопка «Не надо» — отмена. При
    настроенных лекарствах — мультивыбор «Какие лекарства приняли?»: открывается
    пустым, у принятых сегодня — пометка «· сегодня ✓» без галочки (с записью
    связывается только отмеченное в этом выборе). После первой «Да» без
    настроенных лекарств — мягкая подсказка добавить их (один раз).
    Устаревшая кнопка при архивном черновике ничего не планирует (§6.10).
    """
    if await _archive_guard(callback, state):
        return
    measurement_id = await _measurement_id(callback, state)
    if measurement_id is None:
        return
    tg_id = callback.from_user.id
    user = await measurements_service.get_or_create_user(session, tg_id)
    await measurements_service.set_medication_taken(session, user, measurement_id, True)
    await session.commit()
    delay = user.med_remeasure_delay
    if delay:
        await reminders.ensure_remeasure(user.id, measurement_id, delay)
    meds = await medications_service.list_medications(session, user)
    if meds:
        taken_today = await medications_service.taken_intake_ids_today(session, user)
        await state.update_data(
            **{M_MED_CHECKED: [], M_MED_TAKEN_TODAY: sorted(taken_today)}
        )
        markup = keyboards.intake_meds_kb(set(), meds, taken_today)
        if delay:
            # «Напомню…» + «Не надо» — отдельным экраном (у мультивыбора свои
            # кнопки «Готово»/«Отмена»); мультивыбор — следующим сообщением.
            await callback.message.edit_text(  # type: ignore[union-attr]
                texts.MED_REMEASURE_PLANNED.format(delay=delay),
                reply_markup=keyboards.no_remeasure_kb(measurement_id),
            )
            await callback.message.answer(  # type: ignore[union-attr]
                texts.MEDS_INTAKE_ASK, reply_markup=markup
            )
        else:
            await callback.message.edit_text(  # type: ignore[union-attr]
                texts.MEDS_INTAKE_ASK, reply_markup=markup
            )
    else:
        if delay:
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


@router.callback_query(F.data == "m:med:no")
async def cb_med_no(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    """«Нет»: флаг FALSE (§6.3) — назад к сводке."""
    measurement_id = await _measurement_id(callback, state)
    if measurement_id is None:
        return
    tg_id = callback.from_user.id
    user = await measurements_service.get_or_create_user(session, tg_id)
    await measurements_service.set_medication_taken(session, user, measurement_id, False)
    await session.commit()
    data = await state.get_data()
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


# ─────────────────── Мультивыбор «Какие лекарства приняли?» ───────────────────


@router.callback_query(F.data.startswith("m:med:pick:"))
async def cb_med_pick(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    """Переключить отметку приёма (`m:med:pick:{id}`, §6.3).

    Чужой или удалённый id проверяется по владельцу и мягко отклоняется
    (§7.1); экран редактируется на каждое нажатие.
    """
    if await _archive_guard(callback, state):
        return
    data = await state.get_data()
    if M_ID not in data:
        await _to_menu(callback)
        return
    value = callback.data.split(":", 3)[3]  # type: ignore[union-attr]
    if not value.isdigit():
        await _to_menu(callback)
        return
    med_id = int(value)
    tg_id = callback.from_user.id
    user = await measurements_service.get_or_create_user(session, tg_id)
    meds = await medications_service.list_medications(session, user)
    if not any(med.id == med_id for med in meds):
        await callback.answer(texts.NOT_YOUR_RECORD)  # чужой id — мягко, без деталей
        return
    checked = set(data.get(M_MED_CHECKED, []))
    if med_id in checked:
        checked.discard(med_id)
    else:
        checked.add(med_id)
    await state.update_data(**{M_MED_CHECKED: sorted(checked)})
    taken_today = set(data.get(M_MED_TAKEN_TODAY, []))
    await callback.message.edit_text(  # type: ignore[union-attr]
        texts.MEDS_INTAKE_ASK,
        reply_markup=keyboards.intake_meds_kb(checked, meds, taken_today),
    )
    await callback.answer()


@router.callback_query(F.data == "m:med:done")
async def cb_med_done(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    """«Готово»: по одной записи `medication_logs` на отмеченный приём (§6.3).

    Отмеченные приёмы связываются с записью измерения
    (`measurement_medications`, §4.11) — перечень для «Истории записей»
    кабинета (§16.2). Повторное «Готово» за те же локальные сутки новые
    записи и связи не создаёт (§7.2) — идемпотентно. Ответ —
    «💊 Принято: …» + кнопки сводки. При архивном черновике (устаревшая
    кнопка) `medication_logs` не создаются (§6.10).
    """
    if await _archive_guard(callback, state):
        return
    data = await state.get_data()
    if M_ID not in data:
        await _to_menu(callback)
        return
    checked = set(data.get(M_MED_CHECKED, []))
    if not checked:
        await callback.answer(texts.MEDS_INTAKE_EMPTY)  # экран не меняется
        return
    tg_id = callback.from_user.id
    user = await measurements_service.get_or_create_user(session, tg_id)
    # Журнал и связи — одним захватом лока приёмов и одной транзакцией (T1, §7.2).
    result = await medications_service.record_manual_intake(
        session, user, data[M_ID], checked
    )
    await session.commit()
    if not result.taken:  # все id чужие или удалённые
        await callback.answer(texts.MEDS_INTAKE_EMPTY)
        return
    await state.update_data(**{M_MED_CHECKED: []})
    await callback.message.edit_text(  # type: ignore[union-attr]
        texts.fmt_meds_intake(result.taken), reply_markup=keyboards.saved_kb()
    )
    await callback.answer()


@router.callback_query(F.data == "m:med:cancel")
async def cb_med_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    """«Отмена» мультивыбора — назад к сводке без записей (§6.3)."""
    data = await state.get_data()
    if M_ID not in data:
        await _to_menu(callback)
        return
    await _hub(callback.message, data)  # type: ignore[arg-type]
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
    await message.answer(texts.NOTE_SAVED, reply_markup=_saved_markup(data))


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


# ─────────────────── Архивные записи (SPEC.md, §6.10) ───────────────────


async def _archive_begin(
    message: Message, state: FSMContext, *, via_message: bool
) -> None:
    """Начать проход архива с шага «Когда?» (§6.10); прошлый черновик сбрасывается."""
    await state.clear()
    await state.set_state(ArchiveInput.date)
    await _send(
        message, texts.ARCHIVE_DATE_ASK, keyboards.archive_date_kb(), via_message=via_message
    )


@router.callback_query(F.data.in_({"set:archive:add", "m:arch:more"}))
async def cb_archive_begin(callback: CallbackQuery, state: FSMContext) -> None:
    """«Добавить запись» / «➕ Ещё запись» — новый проход с шага «Когда?» (§6.10)."""
    await _archive_begin(callback.message, state, via_message=False)  # type: ignore[arg-type]
    await callback.answer()


@router.callback_query(F.data.startswith("set:archive:date:"))
async def cb_archive_date(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession
) -> None:
    """Шаг «Когда?»: пресеты дат или «Другая дата» (свободный ввод, §6.10).

    Даты считаются в таймзоне пользователя; пресет фиксируется в `A_DATE`,
    дальше — шаг «Во сколько?».
    """
    action = callback.data.split(":", 3)[3]  # type: ignore[union-attr]
    if action == "other":
        await state.set_state(ArchiveInput.date)
        await callback.message.edit_text(  # type: ignore[union-attr]
            texts.ARCHIVE_DATE_OTHER_PROMPT, reply_markup=keyboards.note_cancel_kb()
        )
        await callback.answer()
        return
    offset = keyboards.ARCHIVE_DATE_PRESETS.get(action)
    if offset is None:  # устаревшие кнопки — экран не трогаем (§8, аудит U8)
        await callback.answer(texts.STALE_MESSAGE)
        return
    user = await measurements_service.get_or_create_user(session, callback.from_user.id)
    today = datetime.now(ZoneInfo(user.timezone)).date()
    await state.update_data(**{A_DATE: (today - timedelta(days=offset)).isoformat()})
    await _archive_ask_time(callback.message, state, via_message=False)  # type: ignore[arg-type]
    await callback.answer()


@router.message(ArchiveInput.date)
async def msg_archive_date(
    message: Message, state: FSMContext, session: AsyncSession
) -> None:
    """«Другая дата»: `ДД.ММ[.ГГГГ]` (год — текущий), не позже сегодняшней (§9)."""
    user = await measurements_service.get_or_create_user(
        session, message.from_user.id  # type: ignore[union-attr]
    )
    today = datetime.now(ZoneInfo(user.timezone)).date()
    try:
        day = schemas.parse_archive_date(message.text or "", today)
    except ValueError as exc:
        await message.answer(str(exc), reply_markup=keyboards.note_cancel_kb())
        return
    await state.update_data(**{A_DATE: day.isoformat()})
    await _archive_ask_time(message, state, via_message=True)


async def _archive_ask_time(
    message: Message, state: FSMContext, *, via_message: bool
) -> None:
    """Шаг «Во сколько?» (§6.10)."""
    await state.set_state(ArchiveInput.time)
    await _send(
        message, texts.ARCHIVE_TIME_ASK, keyboards.archive_time_kb(), via_message=via_message
    )


async def _archive_set_moment(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    telegram_id: int,
    moment_time: time,
    *,
    via_message: bool,
) -> Optional[str]:
    """Зафиксировать момент архива (дата + время) и открыть калькулятор (§6.10).

    Возвращает текст ошибки (будущее время) — экран не меняется; при успехе —
    None, показан шаг систолического. Без даты в черновике (устаревшие кнопки) —
    выход в главное меню.
    """
    data = await state.get_data()
    raw_day = data.get(A_DATE)
    if raw_day is None:
        await _send(
            message, texts.MAIN_MENU, keyboards.main_menu(), via_message=via_message
        )
        return None
    user = await measurements_service.get_or_create_user(session, telegram_id)
    try:
        moment = schemas.combine_moment(
            date.fromisoformat(raw_day), moment_time, user.timezone
        )
    except ValueError as exc:
        return str(exc)
    await state.set_state(MeasureInput.sys_input)
    await state.update_data(
        **{
            M_ARCH_TS: moment.astimezone(timezone.utc).isoformat(),
            M_ARCH_LABEL: moment.strftime("%d.%m.%Y, %H:%M"),
            M_BUF: "",
        }
    )
    await _show_keypad(message, state, via_message=via_message)
    return None


@router.callback_query(F.data.startswith("set:archive:time:"))
async def cb_archive_time(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession
) -> None:
    """Шаг «Во сколько?»: пресеты «08:00…» / «Другое» (§6.10).

    Будущее время — «Время измерения не может быть в будущем» (тост), повтор.
    """
    action = callback.data.split(":", 3)[3]  # type: ignore[union-attr]
    if action == "other":
        await state.set_state(ArchiveInput.time)
        await callback.message.edit_text(  # type: ignore[union-attr]
            texts.ARCHIVE_TIME_OTHER_PROMPT, reply_markup=keyboards.note_cancel_kb()
        )
        await callback.answer()
        return
    try:
        moment_time = schemas.validate_time(action)
    except ValueError as exc:
        await callback.answer(str(exc))  # экран не меняется
        return
    error = await _archive_set_moment(
        callback.message,  # type: ignore[arg-type]
        state,
        session,
        callback.from_user.id,
        moment_time,
        via_message=False,
    )
    if error is not None:
        await callback.answer(error)  # экран не меняется
        return
    await callback.answer()


@router.message(ArchiveInput.time)
async def msg_archive_time(
    message: Message, state: FSMContext, session: AsyncSession
) -> None:
    """«Другое» времени: `ЧЧ:ММ`; будущее — ошибка и повтор ввода (§6.10)."""
    try:
        moment_time = schemas.validate_time(message.text or "")
    except ValueError as exc:
        await message.answer(str(exc), reply_markup=keyboards.note_cancel_kb())
        return
    error = await _archive_set_moment(
        message,
        state,
        session,
        message.from_user.id,  # type: ignore[union-attr]
        moment_time,
        via_message=True,
    )
    if error is not None:
        await message.answer(error, reply_markup=keyboards.note_cancel_kb())


@router.callback_query(F.data == "set:archive:quick")
async def cb_archive_quick(callback: CallbackQuery, state: FSMContext) -> None:
    """«Быстрый ввод» — строка `ДД.ММ[.ГГГГ] ЧЧ:ММ САД/ДАД [пульс]` (§6.10)."""
    await state.clear()
    await state.set_state(ArchiveInput.quick)
    await callback.message.edit_text(  # type: ignore[union-attr]
        texts.ARCHIVE_QUICK_PROMPT, reply_markup=keyboards.note_cancel_kb()
    )
    await callback.answer()


@router.message(ArchiveInput.quick)
async def msg_archive_quick(
    message: Message, state: FSMContext, session: AsyncSession
) -> None:
    """Быстрый ввод: разбор строки и INSERT с указанным моментом (§6.10).

    Не распознано — «Не понял формат…» + «Отмена»; дальше — та же сводка
    с кнопками опциональных шагов.
    """
    tg_id = message.from_user.id  # type: ignore[union-attr]
    user = await measurements_service.get_or_create_user(session, tg_id)
    try:
        parsed = schemas.parse_archive_quick(message.text or "", user.timezone)
    except ValueError as exc:
        await message.answer(str(exc), reply_markup=keyboards.note_cancel_kb())
        return
    await state.clear()
    await state.update_data(
        **{
            M_SYS: parsed.systolic,
            M_DIA: parsed.diastolic,
            M_PULSE: parsed.pulse,
            M_ARCH_TS: parsed.moment.astimezone(timezone.utc).isoformat(),
            M_ARCH_LABEL: parsed.moment.strftime("%d.%m.%Y, %H:%M"),
        }
    )
    await _save(message, state, session, tg_id, via_message=True, user=user)


@router.callback_query(F.data == "m:arch:edit")
async def cb_arch_edit(callback: CallbackQuery, state: FSMContext) -> None:
    """«✏️ Изменить» архива — правка только что созданной записи (§6.10).

    Цель правки — id из контекста сохранения (`M_ID`), а не «последняя» по
    `timestamp`: архив можно вносить не в хронологическом порядке.
    """
    data = await state.get_data()
    measurement_id = data.get(M_ID)
    if measurement_id is None:
        await _to_menu(callback)
        return
    await state.clear()
    await state.set_state(MeasureInput.sys_input)
    await state.update_data(
        **{
            M_EDIT_ID: measurement_id,
            M_ID: measurement_id,
            M_BUF: "",
            M_ARCH_TS: data.get(M_ARCH_TS),
            M_ARCH_LABEL: data.get(M_ARCH_LABEL),
        }
    )
    await _show_keypad(callback.message, state)  # type: ignore[arg-type]
    await callback.answer()


@router.callback_query(F.data == "m:arch:note")
async def cb_arch_note(callback: CallbackQuery, state: FSMContext) -> None:
    """«Добавить заметку» архива — тот же экран, что в §6.3."""
    if await _measurement_id(callback, state) is None:
        return
    await state.set_state(MeasureInput.note_input)
    await callback.message.edit_text(  # type: ignore[union-attr]
        texts.PROMPT_NOTE, reply_markup=keyboards.note_cancel_kb()
    )
    await callback.answer()


@router.callback_query(F.data == "m:arch:med")
async def cb_arch_med(callback: CallbackQuery, state: FSMContext) -> None:
    """«Приняли лекарство?» архива — только исторический флаг (§6.10)."""
    if await _measurement_id(callback, state) is None:
        return
    await callback.message.edit_text(  # type: ignore[union-attr]
        texts.MED_ASK, reply_markup=keyboards.archive_medication_taken_kb()
    )
    await callback.answer()


@router.callback_query(F.data.in_({"m:arch:med:yes", "m:arch:med:no"}))
async def cb_arch_med_set(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    """«Да» / «Нет»: только флаг `medication_taken` (§6.10).

    Повторный замер не планируется, выбор лекарств и `medication_logs` не
    создаются — момент приёма в прошлом.
    """
    measurement_id = await _measurement_id(callback, state)
    if measurement_id is None:
        return
    value = callback.data == "m:arch:med:yes"  # type: ignore[union-attr]
    user = await measurements_service.get_or_create_user(session, callback.from_user.id)
    await measurements_service.set_medication_taken(session, user, measurement_id, value)
    await session.commit()
    data = await state.get_data()
    await _hub(callback.message, data)  # type: ignore[arg-type]
    await callback.answer()


@router.callback_query(F.data == "m:arch:feel")
async def cb_arch_feel(callback: CallbackQuery, state: FSMContext) -> None:
    """«Самочувствие» архива — тот же экран, что в §6.3 (кнопки `m:arch:feel:*`)."""
    if await _measurement_id(callback, state) is None:
        return
    await callback.message.edit_text(  # type: ignore[union-attr]
        texts.FEELING_ASK, reply_markup=keyboards.archive_feeling_kb()
    )
    await callback.answer()


@router.callback_query(
    F.data.in_({"m:arch:feel:normal", "m:arch:feel:headache", "m:arch:feel:weakness"})
)
async def cb_arch_feel_set(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    """Самочувствие архивной записи (`m:arch:feel:*`, §6.10) — назад к сводке."""
    if await _measurement_id(callback, state) is None:
        return
    data = await state.get_data()
    feeling = callback.data.split(":", 3)[3]  # type: ignore[union-attr]
    user = await measurements_service.get_or_create_user(session, callback.from_user.id)
    await measurements_service.set_feeling(session, user, data[M_ID], feeling)  # type: ignore[arg-type]
    await session.commit()
    await _hub(callback.message, data)  # type: ignore[arg-type]
    await callback.answer()


@router.callback_query(F.data == "m:arch:feel:other")
async def cb_arch_feel_other(callback: CallbackQuery, state: FSMContext) -> None:
    """«Другое» — короткий текст в заметку (§6.3, §6.10)."""
    if await _measurement_id(callback, state) is None:
        return
    await state.set_state(MeasureInput.feeling_other)
    await callback.message.edit_text(  # type: ignore[union-attr]
        texts.PROMPT_FEELING_OTHER, reply_markup=keyboards.note_cancel_kb()
    )
    await callback.answer()


@router.callback_query(F.data == "m:arch:menu")
async def cb_arch_menu(callback: CallbackQuery, state: FSMContext) -> None:
    """«В меню» архива — сброс черновика и FSM, главное меню (§6.10)."""
    await state.clear()
    await callback.message.edit_text(  # type: ignore[union-attr]
        texts.MAIN_MENU, reply_markup=keyboards.main_menu()
    )
    await callback.answer()
