"""Хендлеры кнопок напоминаний (SPEC.md, §6.7, 0.5.4).

Слот-сообщение (утро/обед/вечер) и повтор замера (med_remeasure) доставляются
планировщиком (`bot/services/reminders.py`); здесь — обработка нажатий:
«Принял всё ✓», «Частично…», «Через 30 мин», «Пропускаю», а также «Больше не
напоминать» повтора замера. «Записать давление» / «Записать сейчас» запускают
сценарий записи (6.3) и обрабатываются в `bot/handlers/measure.py`.

Без FSM (0.5.4): кнопки слота несут слот и дату напоминания
(`rem:all:{slot}:{ГГГГММДД}`), отметки списка «Частично…» — в самой
клавиатуре сообщения (кнопки с «✓ »). Поэтому список переживает перезапуск
бота, любое число открытых списков не путает слоты, а запись давления (FSM
§6.3) ему не мешает. Приём пишется за день напоминания; напоминание старше
2 суток, мусорные данные, чужое или удалённое лекарство — уведомление
«Это напоминание устарело» вместо молчания. Callback'и 0.5.3 без даты
принимаются: дата — день сообщения.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot import keyboards, texts
from bot.models import Medication, User
from bot.services import measurements as measurements_service
from bot.services import medications as medications_service
from bot.services.reminders import RemindersService

router = Router(name="reminders")

#: Напоминание старше стольких суток — «устарело» (§6.7).
STALE_DAYS = 2

#: Повтор замера, доставленный раньше, — «устарело», строка удаляется (§6.7).
REMEASURE_STALE = timedelta(hours=24)


# ─────────────────────────── Разбор callback'ов ───────────────────────────


def _parse_day(raw: str) -> Optional[date]:
    """`ГГГГММДД` → дата; иначе None."""
    if len(raw) != 8 or not raw.isdigit():
        return None
    try:
        return datetime.strptime(raw, "%Y%m%d").date()
    except ValueError:
        return None


def _message_moment(callback: CallbackQuery) -> Optional[datetime]:
    """Момент отправки сообщения с кнопкой (UTC); недоступное сообщение — None."""
    message = callback.message
    moment = getattr(message, "date", None) if isinstance(message, Message) else None
    if not isinstance(moment, datetime) or moment.timestamp() <= 0:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _today(user: User) -> date:
    return datetime.now(ZoneInfo(user.timezone)).date()


def _reminder_day(callback: CallbackQuery, user: User, raw: Optional[str]) -> Optional[date]:
    """День напоминания из callback'а (или дня сообщения для 0.5.3); устаревший — None."""
    if raw is None:
        moment = _message_moment(callback)
        day = moment.astimezone(ZoneInfo(user.timezone)).date() if moment else None
    else:
        day = _parse_day(raw)
    if day is None:
        return None
    today = _today(user)
    if day > today or (today - day).days > STALE_DAYS:
        return None
    return day


def _intake_moment(callback: CallbackQuery, user: User, day: date) -> Optional[datetime]:
    """Момент приёма: сегодня — «сейчас» (None); другой день — момент доставки сообщения.

    Если момент сообщения не внутри дня напоминания (сбой данных) — полдень этого дня.
    """
    if day == _today(user):
        return None
    tz = ZoneInfo(user.timezone)
    moment = _message_moment(callback)
    if moment is not None and moment.astimezone(tz).date() == day:
        return moment
    return datetime.combine(day, time(12, 0), tz).astimezone(timezone.utc)


def _slot_args(callback: CallbackQuery, extra: int = 0) -> Optional[tuple[str, Optional[str], list[str]]]:
    """`rem:{act}:{slot}[:{д}]{:extra…}` → (слот, дата или None, хвост); мусор — None."""
    parts = (callback.data or "").split(":")
    if len(parts) not in (3 + extra, 4 + extra) or parts[2] not in keyboards.SLOT_ORDER:
        return None
    raw_day = parts[3] if len(parts) == 4 + extra else None
    if extra and raw_day is None:
        return None  # список «Частично…» 0.5.4 всегда несёт дату
    return parts[2], raw_day, parts[4:]


def _id_of(callback: CallbackQuery) -> int:
    """Id из callback_data (`rem:off:7` → 7); мусор → 0."""
    raw = (callback.data or "").split(":", 2)[2]
    return int(raw) if raw.isdigit() else 0


async def _stale(callback: CallbackQuery) -> None:
    """Устаревшая или мусорная кнопка — уведомление вместо молчания (§6.7)."""
    await callback.answer(texts.REMINDER_STALE)


async def _finish_screen(callback: CallbackQuery, text: str) -> None:
    """Закрыть диалог действиями: результат + кнопка «В меню» (§6.7)."""
    await callback.message.edit_text(  # type: ignore[union-attr]
        text, reply_markup=keyboards.back_to_menu()
    )
    await callback.answer()


async def _user_and_day(
    callback: CallbackQuery, session: AsyncSession, raw_day: Optional[str]
) -> tuple[User, Optional[date]]:
    user = await measurements_service.get_or_create_user(session, callback.from_user.id)  # type: ignore[arg-type]
    return user, _reminder_day(callback, user, raw_day)


def _med_tuples(meds: list[Medication]) -> list[tuple[int, str, str]]:
    return [(m.id, m.name, m.dosage) for m in meds]


async def _log_and_finish(
    callback: CallbackQuery,
    session: AsyncSession,
    reminders: RemindersService,
    user: User,
    slot: str,
    day: date,
    decisions: dict[int, str],
) -> None:
    """Записать приём за день напоминания и показать итог (§6.7).

    Повтор замера ставится, только если приём сегодняшний и что-то принято.
    """
    result = await medications_service.log_slot_intake(
        session, user, slot, decisions, timestamp=_intake_moment(callback, user, day)
    )
    await session.commit()
    if user.med_remeasure_delay is not None and result.taken and day == _today(user):
        await reminders.ensure_remeasure(user.id, None, user.med_remeasure_delay)
    await _finish_screen(
        callback, texts.fmt_intake_result(result.taken, result.skipped, result.already)
    )


# ─────────────────────────── Слот-сообщение ───────────────────────────


@router.callback_query(F.data.startswith("rem:all:"))
async def cb_take_all(
    callback: CallbackQuery,
    session: AsyncSession,
    reminders: RemindersService,
) -> None:
    """«Принял всё ✓»: все лекарства слота → `taken` за день напоминания (§6.7)."""
    args = _slot_args(callback)
    if args is None:
        await _stale(callback)
        return
    slot, raw_day, _ = args
    user, day = await _user_and_day(callback, session, raw_day)
    meds = await medications_service.slot_medications(session, user, slot) if day else []
    if day is None or not meds:
        await _stale(callback)
        return
    await _log_and_finish(
        callback, session, reminders, user, slot, day, {m.id: "taken" for m in meds}
    )


@router.callback_query(F.data.startswith("rem:partial:"))
async def cb_partial_start(callback: CallbackQuery, session: AsyncSession) -> None:
    """«Частично…»: список лекарств слота, все отмечены (§6.7).

    `rem:partial:done` / `rem:partial:cancel` — кнопки списков 0.5.3 (FSM):
    после обновления — «устарело».
    """
    args = _slot_args(callback)
    if args is None:
        await _stale(callback)
        return
    slot, raw_day, _ = args
    user, day = await _user_and_day(callback, session, raw_day)
    meds = await medications_service.slot_medications(session, user, slot) if day else []
    if day is None or not meds:
        await _stale(callback)
        return
    await callback.message.edit_text(  # type: ignore[union-attr]
        texts.PARTIAL_PROMPT,
        reply_markup=keyboards.partial_meds_kb(
            slot, day, checked={m.id for m in meds}, medications=_med_tuples(meds)
        ),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("rem:pt:"))
async def cb_partial_toggle(callback: CallbackQuery, session: AsyncSession) -> None:
    """Переключить отметку: отметки читаются из клавиатуры сообщения (§6.7)."""
    args = _slot_args(callback, extra=1)
    if args is None or not args[2][0].isdigit():
        await _stale(callback)
        return
    slot, raw_day, tail = args
    med_id = int(tail[0])
    user, day = await _user_and_day(callback, session, raw_day)
    meds = await medications_service.slot_medications(session, user, slot) if day else []
    if day is None or med_id not in {m.id for m in meds}:
        await _stale(callback)  # устарело, чужое или удалённое лекарство (§7.1)
        return
    checked = keyboards.partial_checked_ids(callback.message.reply_markup)  # type: ignore[union-attr]
    checked ^= {med_id}
    await callback.message.edit_text(  # type: ignore[union-attr]
        texts.PARTIAL_PROMPT,
        reply_markup=keyboards.partial_meds_kb(
            slot, day, checked=checked, medications=_med_tuples(meds)
        ),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("rem:pd:"))
async def cb_partial_done(
    callback: CallbackQuery,
    session: AsyncSession,
    reminders: RemindersService,
) -> None:
    """«Готово»: отмеченные → `taken`, остальные лекарства слота → `skipped` (§6.7)."""
    args = _slot_args(callback)
    if args is None or args[1] is None:
        await _stale(callback)
        return
    slot, raw_day, _ = args
    user, day = await _user_and_day(callback, session, raw_day)
    meds = await medications_service.slot_medications(session, user, slot) if day else []
    if day is None or not meds:
        await _stale(callback)
        return
    checked = keyboards.partial_checked_ids(callback.message.reply_markup)  # type: ignore[union-attr]
    decisions = {m.id: ("taken" if m.id in checked else "skipped") for m in meds}
    await _log_and_finish(callback, session, reminders, user, slot, day, decisions)


@router.callback_query(F.data.startswith("rem:pc:"))
async def cb_partial_cancel(callback: CallbackQuery, session: AsyncSession) -> None:
    """«Отмена»: вернуть слот-сообщение с теми же кнопками, ничего не записывая (§6.7)."""
    args = _slot_args(callback)
    if args is None or args[1] is None:
        await _stale(callback)
        return
    slot, raw_day, _ = args
    user, day = await _user_and_day(callback, session, raw_day)
    if day is None:
        await _stale(callback)
        return
    meds = await medications_service.slot_medications(session, user, slot)
    lines = [texts.fmt_med_item(m.name, m.dosage) for m in meds]
    await callback.message.edit_text(  # type: ignore[union-attr]
        texts.fmt_slot_message(slot, lines),
        reply_markup=keyboards.slot_message_kb(slot, day, has_medications=bool(meds)),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("rem:med:"))
async def cb_legacy_partial(callback: CallbackQuery) -> None:
    """Отметки списков 0.5.3 (FSM) — после обновления «устарело» (§8)."""
    await _stale(callback)


@router.callback_query(F.data.startswith("rem:skip:"))
async def cb_skip_slot(
    callback: CallbackQuery,
    session: AsyncSession,
    reminders: RemindersService,
) -> None:
    """«Пропускаю»: лекарства слота → `skipped` за день напоминания (§6.7).

    Сегодняшний слот перепланируется на следующее вхождение.
    """
    args = _slot_args(callback)
    if args is None:
        await _stale(callback)
        return
    slot, raw_day, _ = args
    user, day = await _user_and_day(callback, session, raw_day)
    if day is None:
        await _stale(callback)
        return
    meds = await medications_service.slot_medications(session, user, slot)
    if meds:
        await medications_service.log_slot_intake(
            session,
            user,
            slot,
            {m.id: "skipped" for m in meds},
            timestamp=_intake_moment(callback, user, day),
        )
    await session.commit()
    if day == _today(user):
        await reminders.skip_slot(user.id, slot)
    await _finish_screen(callback, texts.SLOT_SKIPPED)


# ─────────────────────────── «Через 30 мин» ───────────────────────────


def _remeasure_expired(callback: CallbackQuery) -> bool:
    """Сообщение повтора замера доставлено больше 24 часов назад (§6.7)."""
    moment = _message_moment(callback)
    return moment is not None and datetime.now(timezone.utc) - moment > REMEASURE_STALE


@router.callback_query(F.data.startswith("rem:snooze:"))
async def cb_snooze(
    callback: CallbackQuery,
    session: AsyncSession,
    reminders: RemindersService,
) -> None:
    """«Через 30 мин»: слот — `rem:snooze:{slot}:{д}`, повтор — `rem:snooze:{id}` (§6.7).

    Отложить можно только сегодняшнее напоминание; строка слота находится по
    `(users.id, type)`, повтор — по id строки и внутреннему `users.id`.
    """
    args = _slot_args(callback)
    user = await measurements_service.get_or_create_user(session, callback.from_user.id)  # type: ignore[arg-type]
    await session.commit()  # транзакция закрыта до работы сервиса (своя сессия)
    if args is not None:
        slot, raw_day, _ = args
        day = _reminder_day(callback, user, raw_day)
        moved = day == _today(user) and await reminders.snooze_slot(user.id, slot)
    else:
        reminder_id = _id_of(callback)
        if reminder_id <= 0:
            await _stale(callback)
            return
        if _remeasure_expired(callback):
            await reminders.cancel_remeasure(user.id, reminder_id)
            await _stale(callback)
            return
        moved = await reminders.snooze(user.id, reminder_id)
    if not moved:
        await _stale(callback)
        return
    await _finish_screen(callback, texts.SLOT_REMAINING)


# ─────────────────────────── Повтор замера (med_remeasure) ───────────────────────────


@router.callback_query(F.data.startswith("rem:off:"))
async def cb_remeasure_off(
    callback: CallbackQuery,
    session: AsyncSession,
    reminders: RemindersService,
) -> None:
    """«Больше не напоминать»: закрыть этот повтор замера (§6.7).

    Строка повтора (доставленная или отложенная через «Через 30 мин»)
    удаляется, job снимается; настройка «Повтор после лекарства» не меняется.
    Повтор старше 24 часов — тоже удаляется, но с уведомлением «устарело».
    """
    reminder_id = _id_of(callback)
    if reminder_id <= 0:
        await _stale(callback)
        return
    user = await measurements_service.get_or_create_user(session, callback.from_user.id)  # type: ignore[arg-type]
    await session.commit()
    removed = await reminders.cancel_remeasure(user.id, reminder_id)
    if not removed or _remeasure_expired(callback):
        await _stale(callback)
        return
    await _finish_screen(callback, texts.REMEASURE_OFF_DONE)
