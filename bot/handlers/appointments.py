"""«📅 Визиты и анализы» (SPEC.md, §6.13; 0.5.8).

Список, добавление в пять шагов, карточка, правка по полям, отмена и удаление
с подтверждением, кнопки уведомлений («✓ Помню», «Перенести», «📄 Отчёт для
врача», «Отправить врачу»), «Как прошёл визит?» и «Записать следующий?».

Экраны — без FSM: черновик шагов лежит в FSM-data (`ap_*`), состояние
ставится только на шагах свободного ввода (название «Другой…», «Другое
время», заметка — допущение 4). Каждый id из `callback_data` проверяется на
владельца (§7.1); чужое, удалённое или уже отвеченное — «Это сообщение
устарело». Статусы меняются условным `UPDATE` (§7.2).

Режимы черновика (`ap_mode`): `add` — новое событие; `when` — правка даты и
времени (и «Перенести» до события, Н1); `moved` — «Перенёс(ла)» после события;
`title` / `note` — правка названия / заметки (Н4).
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta
from typing import Optional, Union

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot import keyboards, texts
from bot.models import Appointment, User
from bot.services import appointments as ap
from bot.services import measurements as measurements_service
from bot.services import pdf_report

router = Router(name="appointments")

#: Отчёт для врача — за последние столько дней, без выбора периода (В12).
REPORT_DAYS = 30

#: Пользователи, чей PDF сейчас готовится: двойное нажатие — одна выгрузка.
_report_in_progress: set[int] = set()

#: Ключи черновика в FSM-data.
K_MODE, K_ID, K_KIND, K_TITLE, K_DAY, K_TIME, K_STEP = (
    "ap_mode", "ap_id", "ap_kind", "ap_title", "ap_day", "ap_time", "ap_step",
)
DRAFT_KEYS = (K_MODE, K_ID, K_KIND, K_TITLE, K_DAY, K_TIME, K_STEP)


class AppointmentInput(StatesGroup):
    """Свободный ввод: название («Другой…»), «Другое время», заметка."""

    title = State()
    time = State()
    note = State()


Target = Union[CallbackQuery, Message]


# ─────────────────────────── Вспомогательное ───────────────────────────


async def _stale(callback: CallbackQuery) -> None:
    await callback.answer(texts.STALE_MESSAGE)


async def _show(target: Target, text: str, markup) -> None:
    """Callback — правка сообщения; сообщение (свободный ввод) — новый ответ."""
    if isinstance(target, CallbackQuery):
        try:
            await target.message.edit_text(text, reply_markup=markup)  # type: ignore[union-attr]
        except TelegramBadRequest as error:
            if "message is not modified" not in str(error):
                raise
    else:
        await target.answer(text, reply_markup=markup)


async def _user(session: AsyncSession, target: Target) -> User:
    return await measurements_service.get_or_create_user(session, target.from_user.id)  # type: ignore[union-attr]


def _int(raw: str) -> Optional[int]:
    return int(raw) if raw.isdigit() else None


async def _owned(session: AsyncSession, user: User, raw_id: str) -> Optional[Appointment]:
    appointment_id = _int(raw_id)
    return None if appointment_id is None else await ap.get_owned(session, user, appointment_id)


async def _draft(state: FSMContext) -> dict:
    data = await state.get_data()
    return {key: data.get(key) for key in DRAFT_KEYS}


async def _start_draft(state: FSMContext, **values) -> None:
    """Новый черновик: прежние ключи `ap_*` сбрасываются, состояние ввода снимается."""
    data = {k: v for k, v in (await state.get_data()).items() if k not in DRAFT_KEYS}
    data.update(values)
    await state.set_state(None)
    await state.set_data(data)


async def _clear_draft(state: FSMContext) -> None:
    data = {k: v for k, v in (await state.get_data()).items() if k not in DRAFT_KEYS}
    await state.set_state(None)
    await state.set_data(data)


# ─────────────────────────── Экраны ───────────────────────────


def _list_label(appt: Appointment) -> str:
    return f"{texts.AP_ICONS[appt.kind]} {appt.title} · {appt.starts_on:%d.%m}"


async def _list_screen(session: AsyncSession, user: User) -> tuple[str, object]:
    current = ap.today(user)
    upcoming = await ap.list_upcoming(session, user, current)
    _, past_total = await ap.list_past(session, user, current, limit=0)
    lines = [texts.AP_HEADER, ""]
    if upcoming:
        lines.append(texts.AP_SOON)
        lines.extend(ap.fmt_event_block(appt) for appt in upcoming)
    else:
        lines.append(texts.AP_EMPTY)
    markup = keyboards.ap_list_kb([(a.id, _list_label(a)) for a in upcoming], has_past=past_total > 0)
    return "\n".join(lines), markup


async def _past_screen(session: AsyncSession, user: User) -> tuple[str, object]:
    current = ap.today(user)
    rows, total = await ap.list_past(session, user, current)
    lines = [texts.AP_PAST_HEADER, ""]
    lines.extend(
        f"{texts.AP_ICONS[a.kind]} {a.title} — {a.starts_on:%d.%m.%Y} · {ap.status_label(a, current)}"
        for a in rows
    )
    if total > len(rows):
        lines.append(texts.AP_PAST_MORE)
    items = [(a.id, f"{texts.AP_ICONS[a.kind]} {a.title} · {a.starts_on:%d.%m.%Y}") for a in rows]
    return "\n".join(lines), keyboards.ap_past_kb(items)


def _card(appt: Appointment, user: User) -> tuple[str, object]:
    current = ap.today(user)
    if ap.is_upcoming(appt, current):
        card_state, back = "upcoming", "ap:list"
    elif ap.awaiting_answer(appt, current):
        card_state, back = "waiting", "ap:past"
    else:
        card_state, back = "closed", "ap:past"
    return ap.fmt_card(appt, user), keyboards.ap_card_kb(appt.id, appt.kind, card_state, back)


async def _calendar(target: Target, state: FSMContext, user: User, month: Optional[date] = None) -> None:
    """Шаг 3: календарь месяца черновика (или текущего); «Назад» — по режиму."""
    draft = await _draft(state)
    current = ap.today(user)
    shown = month or (date.fromisoformat(draft[K_DAY]) if draft[K_DAY] else current)
    await state.update_data({K_STEP: "day"})
    await _show(
        target,
        texts.AP_DAY_ASK,
        keyboards.ap_calendar_kb(shown.year, shown.month, current, ap.max_day(current), "ap:back"),
    )


async def _time_step(target: Target, state: FSMContext) -> None:
    await state.set_state(None)
    await state.update_data({K_STEP: "time"})
    await _show(target, texts.AP_TIME_ASK, keyboards.ap_time_kb("ap:back"))


async def _note_step(target: Target, state: FSMContext, appt: Optional[Appointment] = None) -> None:
    await state.set_state(AppointmentInput.note)
    await state.update_data({K_STEP: "note"})
    clear_id = appt.id if appt is not None and appt.note else None
    await _show(target, texts.AP_NOTE_ASK, keyboards.ap_note_kb("ap:back", clear_id=clear_id))


async def _title_step(target: Target, state: FSMContext, kind: str) -> None:
    await state.set_state(None)
    await state.update_data({K_STEP: "title"})
    await _show(target, texts.AP_TITLE_ASK[kind], keyboards.ap_title_kb(kind, back="ap:back"))


async def _show_card(target: Target, state: FSMContext, appt: Appointment, user: User) -> None:
    await _clear_draft(state)
    text, markup = _card(appt, user)
    await _show(target, text, markup)


# ─────────────────────────── Список ───────────────────────────


@router.message(Command("visits"))
async def cmd_visits(message: Message, state: FSMContext, session: AsyncSession) -> None:
    """/visits — «📅 Визиты и анализы» (§6.9); сбрасывает FSM."""
    await state.clear()
    user = await _user(session, message)
    text, markup = await _list_screen(session, user)
    await message.answer(text, reply_markup=markup)


@router.callback_query(F.data == "ap:list")
async def cb_list(callback: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    await _clear_draft(state)
    user = await _user(session, callback)
    text, markup = await _list_screen(session, user)
    await _show(callback, text, markup)
    await callback.answer()


@router.callback_query(F.data == "ap:past")
async def cb_past(callback: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    await _clear_draft(state)
    user = await _user(session, callback)
    text, markup = await _past_screen(session, user)
    await _show(callback, text, markup)
    await callback.answer()


# ─────────────────────────── Добавление: шаги 1–2 ───────────────────────────


@router.callback_query(F.data == "ap:add")
async def cb_add(callback: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    """Шаг 1; лимит 30 событий «Скоро» — всплывающее уведомление (В21)."""
    user = await _user(session, callback)
    if await ap.count_upcoming(session, user, ap.today(user)) >= ap.UPCOMING_LIMIT:
        await callback.answer(texts.AP_LIMIT)
        return
    await _start_draft(state, **{K_MODE: "add", K_STEP: "kind"})
    await _show(callback, texts.AP_KIND_ASK, keyboards.ap_kind_kb())
    await callback.answer()


@router.callback_query(F.data.startswith("ap:kind:"))
async def cb_kind(callback: CallbackQuery, state: FSMContext) -> None:
    kind = (callback.data or "").split(":")[2]
    draft = await _draft(state)
    if kind not in texts.AP_KINDS or draft[K_MODE] != "add":
        await _stale(callback)
        return
    await state.update_data({K_KIND: kind})
    await _title_step(callback, state, kind)
    await callback.answer()


async def _title_chosen(target: Target, state: FSMContext, session: AsyncSession, title: str) -> bool:
    """Название выбрано: добавление — к календарю; правка названия — сохранить и карточка."""
    draft = await _draft(state)
    user = await _user(session, target)
    if draft[K_MODE] == "title":
        appt = await ap.get_owned(session, user, draft[K_ID] or 0)
        if appt is None:
            return False
        await ap.set_title(session, appt, title)
        await session.commit()
        await _show_card(target, state, appt, user)
        return True
    if draft[K_MODE] != "add":
        return False
    await state.update_data({K_TITLE: title})
    await state.set_state(None)
    await _calendar(target, state, user)
    return True


@router.callback_query(F.data.startswith("ap:title:"))
async def cb_title(callback: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    """Подсказка `ap:title:{kind}:{n}` или «Другой…» (`ap:title:other`)."""
    parts = (callback.data or "").split(":")
    draft = await _draft(state)
    if draft[K_MODE] not in ("add", "title") or draft[K_KIND] not in texts.AP_KINDS:
        await _stale(callback)
        return
    if parts[2:] == ["other"]:
        await state.set_state(AppointmentInput.title)
        await _show(callback, texts.AP_TITLE_PROMPT, keyboards.ap_back_kb())
        await callback.answer()
        return
    index = _int(parts[3]) if len(parts) == 4 else None
    suggestions = texts.AP_SUGGESTIONS[draft[K_KIND]]
    if parts[2] != draft[K_KIND] or index is None or index >= len(suggestions):
        await _stale(callback)
        return
    if not await _title_chosen(callback, state, session, suggestions[index]):
        await _stale(callback)
        return
    await callback.answer()


@router.message(AppointmentInput.title)
async def msg_title(message: Message, state: FSMContext, session: AsyncSession) -> None:
    title = ap.valid_title(message.text or "")
    if title is None:
        await message.answer(texts.AP_TITLE_TOO_LONG if (message.text or "").strip() else texts.AP_TITLE_PROMPT)
        return
    if not await _title_chosen(message, state, session, title):
        await _clear_draft(state)
        await message.answer(texts.STALE_MESSAGE, reply_markup=keyboards.back_to_menu())


# ─────────────────────────── Шаги 3–4: дата и время ───────────────────────────


@router.callback_query(F.data.startswith("ap:cal:"))
async def cb_calendar_month(callback: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    raw = (callback.data or "").split(":")[2]
    draft = await _draft(state)
    user = await _user(session, callback)
    current = ap.today(user)
    try:
        month = date(int(raw[:4]), int(raw[4:6]), 1) if len(raw) == 6 else None
    except ValueError:
        month = None
    last = ap.max_day(current)
    if (
        month is None
        or draft[K_MODE] not in ("add", "when", "moved")
        or (month.year, month.month) < (current.year, current.month)
        or (month.year, month.month) > (last.year, last.month)
    ):
        await _stale(callback)
        return
    await _calendar(callback, state, user, month)
    await callback.answer()


@router.callback_query(F.data.startswith("ap:day:"))
async def cb_day(callback: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    raw = (callback.data or "").split(":")[2]
    draft = await _draft(state)
    try:
        day = datetime.strptime(raw, "%Y%m%d").date()
    except ValueError:
        day = None
    if day is None or draft[K_MODE] not in ("add", "when", "moved"):
        await _stale(callback)
        return
    user = await _user(session, callback)
    problem = ap.day_problem(user, day)
    if problem is not None:
        await callback.answer(problem)
        return
    await state.update_data({K_DAY: day.isoformat()})
    await _time_step(callback, state)
    await callback.answer()


async def _time_chosen(
    target: Target, state: FSMContext, session: AsyncSession, hhmm: Optional[str]
) -> Optional[str]:
    """Время выбрано: добавление — к заметке; перенос и правка — сохранить. Ошибка — текст."""
    draft = await _draft(state)
    if draft[K_MODE] not in ("add", "when", "moved") or not draft[K_DAY]:
        return texts.STALE_MESSAGE
    user = await _user(session, target)
    day = date.fromisoformat(draft[K_DAY])
    if ap.time_passed(user, day, hhmm):
        return texts.AP_TIME_PASSED
    if draft[K_MODE] == "add":
        await state.update_data({K_TIME: hhmm})
        await _note_step(target, state)
        return None
    appt = await ap.get_owned(session, user, draft[K_ID] or 0)
    if appt is None:
        return texts.STALE_MESSAGE
    if draft[K_MODE] == "when":
        if not ap.is_upcoming(appt, ap.today(user)) or not await ap.reschedule(session, user, appt, day, hhmm):
            return texts.STALE_MESSAGE
        result = appt
    else:
        if not ap.awaiting_answer(appt, ap.today(user)):
            return texts.STALE_MESSAGE
        try:
            result = await ap.moved_after(session, user, appt, day, hhmm)
        except ap.LimitReached:
            await session.rollback()
            return texts.AP_LIMIT
        if result is None:
            return texts.STALE_MESSAGE
    await session.commit()
    await _clear_draft(state)
    await _show(target, ap.fmt_saved(result, user), keyboards.ap_done_kb())
    return None


@router.callback_query(F.data.startswith("ap:time:"))
async def cb_time(callback: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    value = (callback.data or "").split(":")[2]
    if value == "other":
        draft = await _draft(state)
        if draft[K_MODE] not in ("add", "when", "moved") or not draft[K_DAY]:
            await _stale(callback)
            return
        await state.set_state(AppointmentInput.time)
        await _show(callback, texts.AP_TIME_PROMPT, keyboards.ap_back_kb())
        await callback.answer()
        return
    hhmm = None if value == "none" else ap.parse_hhmm(value)
    if value != "none" and (hhmm is None or not 7 <= int(hhmm[:2]) <= 18 or hhmm[3:] != "00"):
        await _stale(callback)
        return
    problem = await _time_chosen(callback, state, session, hhmm)
    await callback.answer(problem)


@router.message(AppointmentInput.time)
async def msg_time(message: Message, state: FSMContext, session: AsyncSession) -> None:
    hhmm = ap.parse_hhmm(message.text or "")
    if hhmm is None:
        await message.answer(texts.AP_TIME_INVALID)
        return
    problem = await _time_chosen(message, state, session, hhmm)
    if problem == texts.AP_TIME_PASSED:
        await message.answer(texts.AP_TIME_PASSED_INPUT)
    elif problem is not None:
        await _clear_draft(state)
        await message.answer(problem, reply_markup=keyboards.back_to_menu())


# ─────────────────────────── Шаг 5: заметка и сохранение ───────────────────────────


async def _note_chosen(target: Target, state: FSMContext, session: AsyncSession, note: Optional[str]) -> Optional[str]:
    """Заметка введена или «Пропустить» (`note=None`). Ошибка — текст."""
    draft = await _draft(state)
    user = await _user(session, target)
    if draft[K_MODE] == "note":
        appt = await ap.get_owned(session, user, draft[K_ID] or 0)
        if appt is None:
            return texts.STALE_MESSAGE
        if note is not None:
            await ap.set_note(session, appt, note)
            await session.commit()
        await _show_card(target, state, appt, user)
        return None
    if draft[K_MODE] != "add" or not (draft[K_KIND] and draft[K_TITLE] and draft[K_DAY]):
        return texts.STALE_MESSAGE
    await _clear_draft(state)  # черновик снят до записи: двойное «Пропустить» — «устарело»
    try:
        appt = await ap.create(
            session, user, draft[K_KIND], draft[K_TITLE], date.fromisoformat(draft[K_DAY]),
            draft[K_TIME], note or "",
        )
    except ap.LimitReached:
        await session.rollback()
        return texts.AP_LIMIT
    await session.commit()
    await _show(target, ap.fmt_saved(appt, user), keyboards.ap_done_kb())
    return None


@router.callback_query(F.data == "ap:note:skip")
async def cb_note_skip(callback: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    problem = await _note_chosen(callback, state, session, None)
    await callback.answer(problem)


@router.message(AppointmentInput.note)
async def msg_note(message: Message, state: FSMContext, session: AsyncSession) -> None:
    note = ap.valid_note(message.text or "")
    if note is None:
        await message.answer(texts.AP_NOTE_TOO_LONG)
        return
    problem = await _note_chosen(message, state, session, note)
    if problem is not None:
        await message.answer(problem, reply_markup=keyboards.back_to_menu())


# ─────────────────────────── «Назад» по шагам ───────────────────────────


@router.callback_query(F.data == "ap:back")
async def cb_back(callback: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    """«Назад» — к предыдущему шагу (из свободного ввода — к кнопкам того же шага).

    Добавление: название → тип, день → название, время → день, заметка → время.
    Правка и перенос: время → день; первый шаг — карточка события.
    """
    draft = await _draft(state)
    mode, step = draft[K_MODE], draft[K_STEP]
    current = await state.get_state()
    user = await _user(session, callback)
    if mode is None:
        await _stale(callback)
        return
    if current == AppointmentInput.title.state:
        await _title_step(callback, state, draft[K_KIND])
        await callback.answer()
        return
    if current == AppointmentInput.time.state:
        await _time_step(callback, state)
        await callback.answer()
        return
    if mode == "add":
        if step == "title":
            await _start_draft(state, **{K_MODE: "add", K_STEP: "kind"})
            await _show(callback, texts.AP_KIND_ASK, keyboards.ap_kind_kb())
        elif step == "day":
            await _title_step(callback, state, draft[K_KIND])
        elif step == "time":
            await _calendar(callback, state, user)
        else:  # note
            await _time_step(callback, state)
        await callback.answer()
        return
    if mode in ("when", "moved") and step == "time":
        await _calendar(callback, state, user)
        await callback.answer()
        return
    appt = await ap.get_owned(session, user, draft[K_ID] or 0)
    if appt is None:
        await _clear_draft(state)
        await _stale(callback)
        return
    await _show_card(callback, state, appt, user)  # первый шаг правки / «Перенёс(ла)» — карточка
    await callback.answer()


# ─────────────────────────── Карточка, правка, удаление ───────────────────────────


@router.callback_query(F.data.startswith("ap:open:"))
async def cb_open(callback: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    user = await _user(session, callback)
    appt = await _owned(session, user, (callback.data or "").split(":")[2])
    if appt is None:
        await _stale(callback)
        return
    await _show_card(callback, state, appt, user)
    await callback.answer()


@router.callback_query(F.data.startswith("ap:edit:"))
async def cb_edit(callback: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    """`ap:edit:{id}` — «Что изменить?»; `…:when` / `…:title` / `…:note` / `…:note:clear` (Н4)."""
    parts = (callback.data or "").split(":")
    user = await _user(session, callback)
    appt = await _owned(session, user, parts[2]) if len(parts) >= 3 else None
    if appt is None or not ap.is_upcoming(appt, ap.today(user)):
        await _stale(callback)
        return
    action = ":".join(parts[3:])
    if action == "":
        await _clear_draft(state)
        await _show(callback, texts.AP_EDIT_ASK, keyboards.ap_edit_kb(appt.id))
    elif action == "when":
        await _start_draft(state, **{K_MODE: "when", K_ID: appt.id, K_DAY: appt.starts_on.isoformat()})
        await _calendar(callback, state, user)
    elif action == "title":
        await _start_draft(state, **{K_MODE: "title", K_ID: appt.id, K_KIND: appt.kind})
        await _title_step(callback, state, appt.kind)
    elif action == "note":
        await _start_draft(state, **{K_MODE: "note", K_ID: appt.id})
        await _note_step(callback, state, appt)
    elif action == "note:clear":
        await ap.set_note(session, appt, "")
        await session.commit()
        await _show_card(callback, state, appt, user)
    else:
        await _stale(callback)
        return
    await callback.answer()


@router.callback_query(F.data.startswith("ap:del:"))
async def cb_delete(callback: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    parts = (callback.data or "").split(":")
    user = await _user(session, callback)
    appt = await _owned(session, user, parts[2])
    if appt is None or parts[3:] not in ([], ["ok"]):
        await _stale(callback)
        return
    if parts[3:] == ["ok"]:
        await ap.remove(session, user, appt.id)
        await session.commit()
        text, markup = await _list_screen(session, user)
        await _show(callback, f"{texts.AP_DELETED}\n\n{text}", markup)
    else:
        await _show(
            callback,
            texts.AP_DELETE_ASK.format(title=appt.title, day=f"{appt.starts_on:%d.%m}"),
            keyboards.ap_confirm_kb(texts.BTN_AP_DELETE_YES, f"ap:del:{appt.id}:ok", f"ap:open:{appt.id}"),
        )
    await callback.answer()


@router.callback_query(F.data.startswith("ap:cancel:"))
async def cb_cancel(callback: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    """«Отменить визит» — подтверждение; «Да, отменить» — `cancelled` (В9)."""
    parts = (callback.data or "").split(":")
    user = await _user(session, callback)
    appt = await _owned(session, user, parts[2])
    if appt is None or not ap.is_upcoming(appt, ap.today(user)) or parts[3:] not in ([], ["ok"]):
        await _stale(callback)
        return
    if parts[3:] == ["ok"]:
        if not await ap.set_status(session, appt, "cancelled"):
            await _stale(callback)
            return
        await session.commit()
        await _clear_draft(state)
        await _show(
            callback,
            texts.AP_CANCELLED.format(title=appt.title, day=f"{appt.starts_on:%d.%m}"),
            keyboards.ap_done_kb(),
        )
    else:
        await _show(
            callback,
            texts.AP_CANCEL_ASK.format(event=ap.fmt_event(appt, icon=False)),
            keyboards.ap_confirm_kb(texts.BTN_AP_CANCEL_YES, f"ap:cancel:{appt.id}:ok", f"ap:open:{appt.id}"),
        )
    await callback.answer()


# ─────────────────────────── Кнопки уведомлений ───────────────────────────


@router.callback_query(F.data.startswith("ap:seen:"))
async def cb_seen(callback: CallbackQuery, session: AsyncSession) -> None:
    """«✓ Помню» — снять кнопки, ничего не менять."""
    user = await _user(session, callback)
    if await _owned(session, user, (callback.data or "").split(":")[2]) is None:
        await _stale(callback)
        return
    try:
        await callback.message.edit_reply_markup(reply_markup=None)  # type: ignore[union-attr]
    except TelegramBadRequest:
        pass
    await callback.answer()


@router.callback_query(F.data.startswith("ap:move:"))
async def cb_move(callback: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    """«Перенести» до события — шаги 3–4, та же строка (Н1)."""
    user = await _user(session, callback)
    appt = await _owned(session, user, (callback.data or "").split(":")[2])
    if appt is None or not ap.is_upcoming(appt, ap.today(user)):
        await _stale(callback)
        return
    await _start_draft(state, **{K_MODE: "when", K_ID: appt.id, K_DAY: appt.starts_on.isoformat()})
    await _calendar(callback, state, user)
    await callback.answer()


@router.callback_query(F.data.startswith("ap:after:"))
async def cb_after(callback: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    """«Как прошёл визит?»: done / moved / missed (В10, Н1, Н2)."""
    parts = (callback.data or "").split(":")
    user = await _user(session, callback)
    appt = await _owned(session, user, parts[2]) if len(parts) == 4 else None
    answer = parts[3] if len(parts) == 4 else ""
    if appt is None or answer not in ("done", "moved", "missed") or not ap.awaiting_answer(appt, ap.today(user)):
        await _stale(callback)
        return
    if answer == "moved":
        await _start_draft(state, **{K_MODE: "moved", K_ID: appt.id})
        await _calendar(callback, state, user)
        await callback.answer()
        return
    if not await ap.set_status(session, appt, answer):
        await _stale(callback)
        return
    await session.commit()
    if answer == "done":
        await _show(callback, texts.AP_DONE, keyboards.ap_next_kb(appt.id))
    else:
        await _show(callback, texts.AP_MISSED, keyboards.ap_done_kb())
    await callback.answer()


@router.callback_query(F.data.startswith("ap:next:"))
async def cb_next(callback: CallbackQuery, session: AsyncSession) -> None:
    """«Записать следующий?» — +1 / 3 / 6 / 12 месяцев или «Не нужно» (В11)."""
    parts = (callback.data or "").split(":")
    user = await _user(session, callback)
    appt = await _owned(session, user, parts[2]) if len(parts) == 4 else None
    months = _int(parts[3]) if len(parts) == 4 else None
    if appt is None or appt.status != "done" or months not in (0, 1, 3, 6, 12):
        await _stale(callback)
        return
    if months == 0:
        await _show(callback, texts.AP_NEXT_NO, keyboards.ap_done_kb())
        await callback.answer()
        return
    try:
        created = await ap.create_next(session, user, appt, months)
    except ap.LimitReached:
        await session.rollback()
        await callback.answer(texts.AP_LIMIT)
        return
    if created is None:
        await _stale(callback)
        return
    await session.commit()
    await _show(callback, ap.fmt_saved(created, user), keyboards.ap_done_kb())
    await callback.answer()


@router.callback_query(F.data.startswith("ap:report:"))
async def cb_report(callback: CallbackQuery, session: AsyncSession) -> None:
    """«📄 Отчёт для врача» — PDF за последние 30 дней отдельным документом (В12)."""
    user = await _user(session, callback)
    if await _owned(session, user, (callback.data or "").split(":")[2]) is None:
        await _stale(callback)
        return
    tg_id = callback.from_user.id
    if tg_id in _report_in_progress:
        await callback.answer(texts.PDF_PREPARING)
        return
    _report_in_progress.add(tg_id)
    try:
        end = ap.today(user)
        start = end - timedelta(days=REPORT_DAYS - 1)
        try:
            data = await pdf_report.collect_report(
                session, user, start, end, subject=pdf_report.patient_subject(user)
            )
        except pdf_report.ReportEmptyError:
            await session.commit()
            await callback.answer(texts.PDF_EMPTY)
            return
        except pdf_report.ReportTooLargeError:
            await session.commit()
            await callback.answer(texts.CSV_TOO_MANY)
            return
        await session.commit()  # транзакция закрыта до генерации и отправки (§7.2)
        await callback.answer(texts.PDF_PREPARING)
        payload = await asyncio.to_thread(pdf_report.build_pdf, data)
        await callback.message.answer_document(  # type: ignore[union-attr]
            BufferedInputFile(payload, filename=data.file_name),
            caption=texts.PDF_FILE_CAPTION.format(
                date_from=f"{data.start_day:%d.%m}", date_to=f"{data.end_day:%d.%m}"
            ),
        )
    finally:
        _report_in_progress.discard(tg_id)


@router.callback_query(F.data.startswith("ap:send:"))
async def cb_send(callback: CallbackQuery, session: AsyncSession, bot: Bot) -> None:
    """«Отправить врачу» — сценарий «Отправить отчет» (§21) новым сообщением (В12)."""
    from bot.handlers.doctor import report_text

    user = await _user(session, callback)
    if await _owned(session, user, (callback.data or "").split(":")[2]) is None:
        await _stale(callback)
        return
    text = await report_text(session, callback, bot)
    await callback.message.answer(  # type: ignore[union-attr]
        text, reply_markup=keyboards.back_to_menu(), link_preview_options=keyboards.NO_LINK_PREVIEW
    )
    await callback.answer()


