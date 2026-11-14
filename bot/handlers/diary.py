"""Хендлер «Дневник за сегодня» (SPEC.md, §6.4; 0.6.0).

Лента событий дня: измерения (`measurements`), отметки приёмов
(`medication_logs`) и события (`diary_events`) — по времени вперемешку
(R23 = А), со строками-кнопками: событие → карточка (`ev:open`), приём —
тост «Только просмотр» (M4), измерение — без действий (`d:noop`, M6).
Чипы-фильтры «Давление» / «Лекарства» / «Факторы» / «Другое» (M2 + C11);
«Все» — отсутствие выделения (M1); «Всего записей: N» — по текущему фильтру
(V11). Сообщение редактируется, черновик FSM не трогается: просмотр
дневника не отменяет начатый ввод (фильтр — отдельный ключ `d_filter`).

Лекарства на сегодня (0.5.5, U6): строка «💊 Лекарства: утро ✓ · …» по слотам
и «💊 Отметить приём» — список слота без FSM; «Готово» и «Отмена» возвращают
дневник.
"""
from __future__ import annotations

import json
from datetime import date, datetime
from typing import Optional
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery
from sqlalchemy.ext.asyncio import AsyncSession

from bot import keyboards, texts
from bot.models import Measurement, User
from bot.services import appointments as appointments_service
from bot.services import events as events_service
from bot.services import measurements as measurements_service
from bot.services import medications as medications_service
from bot.services.reminders import RemindersService

router = Router(name="diary")

#: Активный фильтр ленты в FSM-данных (не трогает черновик ввода, §6.4).
D_FILTER = "d_filter"

#: Фильтры → типы событий (M2 + C11): «Факторы» — food/stress/sleep,
#: «Другое» — activity/note.
FILTER_EVENT_TYPES = {
    "fact": ("food", "stress", "sleep"),
    "other": ("activity", "note"),
}


def _line(measurement: Measurement, tz_name: str, *, large: bool = False) -> str:
    """Строка дневника: локальное время + показатели и флаги (§6.4)."""
    local = measurement.timestamp.astimezone(ZoneInfo(tz_name))
    return texts.fmt_diary_line(
        local.strftime("%H:%M"),
        measurement.systolic,
        measurement.diastolic,
        measurement.pulse,
        measurement.medication_taken,
        measurement.feeling,
        large=large,
    )


def _today(user: User) -> date:
    return datetime.now(ZoneInfo(user.timezone)).date()


def _meds_line(statuses: list[tuple[str, str]]) -> str:
    """«💊 Лекарства: утро ✓ · обед — частично · вечер — ещё не отмечено» (§6.4)."""
    parts = [
        texts.DIARY_MEDS_STATUS[status].format(slot=texts.SLOT_LABELS[slot].lower())
        for slot, status in statuses
    ]
    return texts.DIARY_MEDS_PREFIX + " · ".join(parts)


def _unmarked(statuses: list[tuple[str, str]]) -> list[str]:
    """Слоты, где не все лекарства приняты (их можно отметить, §6.4)."""
    return [slot for slot, status in statuses if status != "taken"]


def _match_filter(item: events_service.FeedItem, filter_: Optional[str]) -> bool:
    """Строка ленты проходит фильтр-чип (M2 + C11)."""
    if filter_ is None:
        return True
    if filter_ in ("bp", "med"):
        return item.kind == filter_
    return item.kind == "event" and item.event.type in FILTER_EVENT_TYPES[filter_]


def _feed_row(
    item: events_service.FeedItem,
    meds_by_id: dict[int, object],
    user: User,
) -> tuple[str, str]:
    """Строка ленты (текст кнопки, callback) — §6.4, I2, M5, M7.

    Кнопка всегда однострочна: блочный формат крупного режима к строкам
    ленты не применяется (§11 — про текстовые списки).
    """
    local = item.moment.astimezone(ZoneInfo(user.timezone))
    time_str = local.strftime("%H:%M")
    if item.kind == "bp":
        return _line(item.measurement, user.timezone), "d:noop"
    if item.kind == "med":
        med = meds_by_id.get(item.log.medication_id)
        name = med.name if med else "?"
        dosage = med.dosage if med else ""
        text = texts.fmt_diary_med_line(
            time_str, name, dosage, item.log.slot, item.log.status
        )
        return text, f"d:medview:{item.log.id}"
    event = item.event
    emoji = texts.EVENT_EMOJI.get(event.type, "📝")
    if event.type == "note":
        text = texts.fmt_diary_note_line(time_str, event.note or "")
        return text, f"ev:open:{event.id}"
    payload = json.loads(event.payload) if event.payload else {}
    kind = texts.event_kind_line(event.type, payload)
    if event.type == "sleep":
        return f"{emoji} {kind}", f"ev:open:{event.id}"  # без времени (C22)
    return texts.fmt_diary_event_line(time_str, kind, emoji), f"ev:open:{event.id}"


async def _diary_screen(
    session: AsyncSession,
    user: User,
    header: Optional[str] = None,
    *,
    filter_: Optional[str] = None,
    large: bool = False,
) -> tuple[str, object]:
    """Лента дня: шапка, строки-кнопки, чипы фильтров, действия (§6.4, 0.6.0).

    «Всего записей: N» — по текущему фильтру (V11); в пустом фильтре —
    «В этой категории записей нет»; день без записей — «Сегодня записей ещё нет».
    """
    today = _today(user)
    feed = await events_service.day_feed(session, user, today)
    meds_by_id = {m.id: m for m in await medications_service.list_medications(session, user)}
    statuses = await medications_service.day_slot_statuses(session, user, today)
    rows = [item for item in feed if _match_filter(item, filter_)]
    feed_rows = [_feed_row(item, meds_by_id, user) for item in rows]
    tail: list[str] = []
    if not feed:
        tail.append(texts.DIARY_EMPTY)
    elif not rows:
        tail.append(texts.DIARY_FILTER_EMPTY)
    else:
        tail.append(texts.DIARY_TOTAL.format(count=len(rows)))
    if statuses:
        tail.append(_meds_line(statuses))
    events_line = appointments_service.diary_line(
        *await appointments_service.day_events(session, user, today)
    )
    if events_line:
        tail.append(events_line)  # ближайшее событие (§6.4, 0.5.8)
    sections = [texts.screen_header("diary")]
    if header:
        sections.append(header)
    sections.append("\n".join(tail))
    text = ("\n\n" if large else "\n").join(sections)
    markup = keyboards.diary_kb(
        feed_rows,
        active_filter=filter_,
        is_empty=not feed,
        mark_intake=bool(_unmarked(statuses)),
    )
    return text, markup


@router.callback_query(F.data == "menu:diary")
async def cb_diary(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession
) -> None:
    """«Дневник за сегодня» — лента за локальные сутки (сообщение редактируется)."""
    user = await measurements_service.get_or_create_user(session, callback.from_user.id)
    filter_ = (await state.get_data()).get(D_FILTER)
    text, markup = await _diary_screen(session, user, filter_=filter_, large=user.ui_large)
    await callback.message.edit_text(  # type: ignore[union-attr]
        text, reply_markup=markup, parse_mode="HTML"
    )
    await callback.answer()


@router.callback_query(F.data.startswith("d:filt:"))
async def cb_diary_filter(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession
) -> None:
    """Чип фильтра: тап по активному сбрасывает в «Все» (M1)."""
    key = callback.data.split(":", 2)[2]  # type: ignore[union-attr]
    if key not in texts.DIARY_FILTER_LABELS:
        await callback.answer(texts.STALE_MESSAGE)
        return
    data = await state.get_data()
    filter_ = None if data.get(D_FILTER) == key else key
    await state.update_data(**{D_FILTER: filter_})
    user = await measurements_service.get_or_create_user(session, callback.from_user.id)
    text, markup = await _diary_screen(session, user, filter_=filter_, large=user.ui_large)
    await callback.message.edit_text(  # type: ignore[union-attr]
        text, reply_markup=markup, parse_mode="HTML"
    )
    await callback.answer()


@router.callback_query(F.data == "d:noop")
async def cb_diary_noop(callback: CallbackQuery) -> None:
    """Тап по строке измерения — ничего (M6): правка — кнопкой «Изменить последнюю»."""
    await callback.answer()


@router.callback_query(F.data.startswith("d:medview:"))
async def cb_diary_med_view(callback: CallbackQuery) -> None:
    """Тап по строке приёма — «Только просмотр» (M4): правки из ленты нет."""
    await callback.answer(texts.DIARY_MED_VIEW)


# ─────────────────────────── «💊 Отметить приём» (§6.4, 0.5.5) ───────────────────────────


async def _stale(callback: CallbackQuery) -> None:
    await callback.answer(texts.STALE_MESSAGE)


async def _show_list(callback: CallbackQuery, session: AsyncSession, user: User, slot: str) -> None:
    """Список лекарств слота, все отмечены; дата — сегодня (§6.4)."""
    meds = await medications_service.slot_medications(session, user, slot)
    await callback.message.edit_text(  # type: ignore[union-attr]
        texts.PARTIAL_PROMPT,
        reply_markup=keyboards.partial_meds_kb(
            slot,
            _today(user),
            checked={m.id for m in meds},
            medications=[(m.id, m.name, m.dosage) for m in meds],
            area="d",
        ),
    )
    await callback.answer()


@router.callback_query((F.data == "d:mt") | F.data.startswith("d:mt:"))
async def cb_mark_intake(callback: CallbackQuery, session: AsyncSession) -> None:
    """«💊 Отметить приём»: один неотмеченный слот — сразу список, несколько — выбор."""
    user = await measurements_service.get_or_create_user(session, callback.from_user.id)
    statuses = await medications_service.day_slot_statuses(session, user, _today(user))
    unmarked = _unmarked(statuses)
    parts = (callback.data or "").split(":")
    if len(parts) == 3:
        if parts[2] not in unmarked:
            await _stale(callback)  # слот уже отмечен, без лекарств или мусор
            return
        await _show_list(callback, session, user, parts[2])
        return
    if not unmarked:
        await _stale(callback)
        return
    if len(unmarked) == 1:
        await _show_list(callback, session, user, unmarked[0])
        return
    await callback.message.edit_text(  # type: ignore[union-attr]
        texts.MARK_INTAKE_ASK, reply_markup=keyboards.mark_intake_slots_kb(unmarked)
    )
    await callback.answer()


def _list_args(callback: CallbackQuery, user: User, extra: int = 0) -> Optional[tuple[str, list[str]]]:
    """`d:{act}:{slot}:{д}{:extra}` → (слот, хвост); дата не сегодня или мусор — None."""
    parts = (callback.data or "").split(":")
    if len(parts) != 4 + extra or parts[2] not in medications_service.DAY_SLOTS:
        return None
    if parts[3] != _today(user).strftime("%Y%m%d"):
        return None
    return parts[2], parts[4:]


@router.callback_query(F.data.startswith("d:pt:"))
async def cb_diary_toggle(callback: CallbackQuery, session: AsyncSession) -> None:
    """Переключить отметку: отметки — в клавиатуре сообщения (§6.4)."""
    user = await measurements_service.get_or_create_user(session, callback.from_user.id)
    args = _list_args(callback, user, extra=1)
    if args is None or not args[1][0].isdigit():
        await _stale(callback)
        return
    slot, tail = args
    meds = await medications_service.slot_medications(session, user, slot)
    med_id = int(tail[0])
    if med_id not in {m.id for m in meds}:
        await _stale(callback)  # чужое или удалённое лекарство (§7.1)
        return
    checked = keyboards.partial_checked_ids(callback.message.reply_markup)  # type: ignore[union-attr]
    checked ^= {med_id}
    await callback.message.edit_text(  # type: ignore[union-attr]
        texts.PARTIAL_PROMPT,
        reply_markup=keyboards.partial_meds_kb(
            slot, _today(user), checked=checked,
            medications=[(m.id, m.name, m.dosage) for m in meds], area="d",
        ),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("d:pd:"))
async def cb_diary_done(
    callback: CallbackQuery, session: AsyncSession, reminders: RemindersService
) -> None:
    """«Готово»: отмеченные — `taken` (в т.ч. из «пропущено»), остальные — `skipped`."""
    user = await measurements_service.get_or_create_user(session, callback.from_user.id)
    args = _list_args(callback, user)
    meds = await medications_service.slot_medications(session, user, args[0]) if args else []
    if args is None or not meds:
        await _stale(callback)
        return
    checked = keyboards.partial_checked_ids(callback.message.reply_markup)  # type: ignore[union-attr]
    decisions = {m.id: ("taken" if m.id in checked else "skipped") for m in meds}
    result = await medications_service.log_slot_intake(
        session, user, args[0], decisions, upgrade_skipped=True
    )
    await session.commit()
    if user.med_remeasure_delay is not None and result.taken:
        await reminders.ensure_remeasure(user.id, None, user.med_remeasure_delay)
    header = texts.fmt_intake_result(result.taken, result.skipped, result.already)
    text, markup = await _diary_screen(session, user, header=header, large=user.ui_large)
    await callback.message.edit_text(  # type: ignore[union-attr]
        text, reply_markup=markup, parse_mode="HTML"
    )
    await callback.answer()


@router.callback_query(F.data.startswith("d:pc:"))
async def cb_diary_cancel(callback: CallbackQuery, session: AsyncSession) -> None:
    """«Отмена»: дневник без изменений (§6.4)."""
    user = await measurements_service.get_or_create_user(session, callback.from_user.id)
    if _list_args(callback, user) is None:
        await _stale(callback)
        return
    text, markup = await _diary_screen(session, user, large=user.ui_large)
    await callback.message.edit_text(  # type: ignore[union-attr]
        text, reply_markup=markup, parse_mode="HTML"
    )
    await callback.answer()
