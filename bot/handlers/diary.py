"""Хендлер «Дневник за сегодня» (SPEC.md, §6.4).

Список измерений текущих локальных суток пользователя с итогом
«Всего записей: N»; пусто — «Сегодня записей ещё нет» + «Записать давление»,
при непустом списке — кнопка «✏️ Изменить последнюю» (правка последней записи,
§6.3; кнопка приходит через `diary_kb()`, обрабатывается в `measure.py`).
Сообщение редактируется (`edit_message_text`), FSM не трогается: просмотр
дневника не отменяет начатый ввод.

Лекарства на сегодня (0.5.5, U6): строка «💊 Лекарства: утро ✓ · …» по слотам
с лекарствами и «💊 Отметить приём» — список слота без FSM (тот же механизм,
что «Отметить принятые» в напоминании, §6.7), «Готово» и «Отмена» возвращают
дневник. Повторная отметка переводит «пропущено» в «принято».
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Optional
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.types import CallbackQuery
from sqlalchemy.ext.asyncio import AsyncSession

from bot import keyboards, texts
from bot.models import Measurement, User
from bot.services import appointments as appointments_service
from bot.services import measurements as measurements_service
from bot.services import medications as medications_service
from bot.services.reminders import RemindersService

router = Router(name="diary")


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


async def _diary_screen(
    session: AsyncSession,
    user: User,
    header: Optional[str] = None,
    *,
    large: bool = False,
) -> tuple[str, object]:
    """Экран дневника: шапка, записи, строка лекарств, кнопки (§6.4, мокап 2.3).

    `header` — итог отметки приёма (HTML-текст, экранированный форматтером).
    Крупный режим: записи блоками, секции разделяются пустой строкой.
    """
    today = _today(user)
    rows = await measurements_service.measurements_for_day(session, user, today)
    statuses = await medications_service.day_slot_statuses(session, user, today)
    tail: list[str] = []
    if not rows:
        tail.append(texts.DIARY_EMPTY)
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
    if rows:
        entries = [_line(row, user.timezone, large=large) for row in rows]
        sections.append("\n\n".join(entries) if large else "\n".join(entries))
    sections.append("\n".join(tail))
    text = ("\n\n" if large else "\n").join(sections)
    markup = keyboards.diary_kb(is_empty=not rows, mark_intake=bool(_unmarked(statuses)))
    return text, markup


@router.callback_query(F.data == "menu:diary")
async def cb_diary(callback: CallbackQuery, session: AsyncSession) -> None:
    """«Дневник за сегодня» — список за локальные сутки (сообщение редактируется)."""
    user = await measurements_service.get_or_create_user(session, callback.from_user.id)
    text, markup = await _diary_screen(session, user, large=user.ui_large)
    await callback.message.edit_text(  # type: ignore[union-attr]
        text, reply_markup=markup, parse_mode="HTML"
    )
    await callback.answer()


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
