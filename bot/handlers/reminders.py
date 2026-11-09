"""Хендлеры кнопок напоминаний (SPEC.md, §6.7).

Слот-сообщение (утро/обед/вечер) и повтор замера (med_remeasure) доставляются
планировщиком (`bot/services/reminders.py`); здесь — обработка нажатий:
«Принял всё ✓», «Частично…», «Через 30 мин», «Пропускаю», а также «Больше не
напоминать» повтора замера. «Записать давление» / «Записать сейчас» запускают
сценарий записи (6.3) и обрабатываются в `bot/handlers/measure.py`.

Callback_data несёт слот (`rem:all:{slot}`) или id строки напоминания
(`rem:snooze:{id}`) — обработчики действуют строго по ним; чужой или мусорный
идентификатор тихо игнорируется (§7.1). Состояние чекбоксов «Частично…» живёт
в FSM-данных диалога и очищается по «Готово» / «Отмена».
"""
from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery
from sqlalchemy.ext.asyncio import AsyncSession

from bot import keyboards, texts
from bot.services import measurements as measurements_service
from bot.services import medications as medications_service
from bot.services.reminders import RemindersService

router = Router(name="reminders")

#: Ключи FSM-данных диалога «Частично…».
P_PARTIAL_SLOT = "partial_slot"
P_PARTIAL_CHECKED = "partial_checked"


def _slot_of(callback: CallbackQuery) -> str:
    """Слот из callback_data (`rem:all:morning` → `morning`); мусор → «»."""
    return (callback.data or "").split(":", 2)[2]


def _id_of(callback: CallbackQuery) -> int:
    """Id из callback_data (`rem:snooze:7` → 7); мусор → 0."""
    raw = (callback.data or "").split(":", 2)[2]
    return int(raw) if raw.isdigit() else 0


async def _finish_screen(callback: CallbackQuery, text: str) -> None:
    """Закрыть диалог действиями: результат + кнопка «В меню» (§6.7)."""
    await callback.message.edit_text(  # type: ignore[union-attr]
        text, reply_markup=keyboards.back_to_menu()
    )
    await callback.answer()


# ─────────────────────────── Слот-сообщение ───────────────────────────


@router.callback_query(F.data.startswith("rem:all:"))
async def cb_take_all(
    callback: CallbackQuery,
    session: AsyncSession,
    state: FSMContext,
    reminders: RemindersService,
) -> None:
    """«Принял всё ✓»: все лекарства слота → `taken` одной транзакцией (§6.7).

    Если включён «Повтор после лекарства» — планируется повторный замер
    (идемпотентно, без привязки к записи). Повторное нажатие ничего не плодит.
    """
    slot = _slot_of(callback)
    if slot not in keyboards.SLOT_ORDER:
        await callback.answer()
        return
    user = await measurements_service.get_or_create_user(session, callback.from_user.id)  # type: ignore[arg-type]
    meds = await medications_service.slot_medications(session, user, slot)
    if not meds:
        await callback.answer()
        return
    result = await medications_service.log_slot_intake(
        session, user, slot, {medication.id: "taken" for medication in meds}
    )
    await session.commit()
    if user.med_remeasure_delay is not None and result.taken:
        await reminders.ensure_remeasure(user.id, None, user.med_remeasure_delay)
    await state.clear()
    summary = texts.fmt_meds_summary(result.taken, result.skipped)
    await _finish_screen(callback, texts.MEDS_LOGGED.format(summary=summary))


@router.callback_query(
    F.data.in_({f"rem:partial:{slot}" for slot in keyboards.SLOT_ORDER})
)
async def cb_partial_start(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    """«Частично…»: чекбоксы лекарств слота (§6.7).

    Состояние набора живёт в FSM-данных: «Готово» логирует `taken` для
    отмеченных и `skipped` для остальных.
    """
    slot = _slot_of(callback)
    if slot not in keyboards.SLOT_ORDER:
        await callback.answer()
        return
    user = await measurements_service.get_or_create_user(session, callback.from_user.id)  # type: ignore[arg-type]
    meds = await medications_service.slot_medications(session, user, slot)
    if not meds:
        await callback.answer()
        return
    await state.update_data(
        **{P_PARTIAL_SLOT: slot, P_PARTIAL_CHECKED: [medication.id for medication in meds]}
    )
    medications = [(m.id, m.name, m.dosage) for m in meds]
    await callback.message.edit_text(  # type: ignore[union-attr]
        texts.PARTIAL_PROMPT,
        reply_markup=keyboards.partial_meds_kb(
            checked={medication.id for medication in meds}, medications=medications
        ),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("rem:med:"))
async def cb_partial_toggle(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    """Переключить чекбокс лекарства в диалоге «Частично…» (§6.7)."""
    med_id = _id_of(callback)
    data = await state.get_data()
    slot = data.get(P_PARTIAL_SLOT)
    if med_id <= 0 or slot not in keyboards.SLOT_ORDER:
        await callback.answer()
        return
    user = await measurements_service.get_or_create_user(session, callback.from_user.id)  # type: ignore[arg-type]
    meds = await medications_service.slot_medications(session, user, slot)
    known = {medication.id for medication in meds}
    if med_id not in known:
        await callback.answer()
        return
    checked = set(data.get(P_PARTIAL_CHECKED, []))
    if med_id in checked:
        checked.discard(med_id)
    else:
        checked.add(med_id)
    await state.update_data(**{P_PARTIAL_CHECKED: sorted(checked)})
    medications = [(m.id, m.name, m.dosage) for m in meds]
    await callback.message.edit_text(  # type: ignore[union-attr]
        texts.PARTIAL_PROMPT,
        reply_markup=keyboards.partial_meds_kb(checked=checked, medications=medications),
    )
    await callback.answer()


@router.callback_query(F.data == "rem:partial:done")
async def cb_partial_done(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    reminders: RemindersService,
) -> None:
    """«Готово»: отмеченные → `taken`, остальные лекарства слота → `skipped` (§6.7)."""
    data = await state.get_data()
    slot = data.get(P_PARTIAL_SLOT)
    if slot not in keyboards.SLOT_ORDER:
        await state.clear()
        await callback.answer()
        return
    user = await measurements_service.get_or_create_user(session, callback.from_user.id)  # type: ignore[arg-type]
    meds = await medications_service.slot_medications(session, user, slot)
    if not meds:
        await state.clear()
        await callback.answer()
        return
    checked = set(data.get(P_PARTIAL_CHECKED, []))
    decisions = {
        medication.id: ("taken" if medication.id in checked else "skipped")
        for medication in meds
    }
    result = await medications_service.log_slot_intake(session, user, slot, decisions)
    await session.commit()
    if user.med_remeasure_delay is not None and result.taken:
        await reminders.ensure_remeasure(user.id, None, user.med_remeasure_delay)
    await state.clear()
    summary = texts.fmt_meds_summary(result.taken, result.skipped)
    await _finish_screen(callback, texts.MEDS_LOGGED.format(summary=summary))


@router.callback_query(F.data == "rem:partial:cancel")
async def cb_partial_cancel(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    """«Отмена» в «Частично…»: вернуть слот-сообщение, ничего не логируя (§6.7)."""
    data = await state.get_data()
    slot = data.get(P_PARTIAL_SLOT)
    await state.clear()
    if slot not in keyboards.SLOT_ORDER:
        await callback.answer()
        return
    user = await measurements_service.get_or_create_user(session, callback.from_user.id)  # type: ignore[arg-type]
    meds = await medications_service.slot_medications(session, user, slot)
    lines = [texts.fmt_med_item(m.name, m.dosage) for m in meds]
    await callback.message.edit_text(  # type: ignore[union-attr]
        texts.fmt_slot_message(slot, lines),
        reply_markup=keyboards.slot_message_kb(slot, 0, has_medications=bool(meds)),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("rem:skip:"))
async def cb_skip_slot(
    callback: CallbackQuery,
    session: AsyncSession,
    state: FSMContext,
    reminders: RemindersService,
) -> None:
    """«Пропускаю»: лекарства слота → `skipped`, слот перепланируется на завтра (§6.7)."""
    slot = _slot_of(callback)
    if slot not in keyboards.SLOT_ORDER:
        await callback.answer()
        return
    user = await measurements_service.get_or_create_user(session, callback.from_user.id)  # type: ignore[arg-type]
    meds = await medications_service.slot_medications(session, user, slot)
    if meds:
        await medications_service.log_slot_intake(
            session, user, slot, {medication.id: "skipped" for medication in meds}
        )
        await session.commit()
    await reminders.skip_slot(user.id, slot)
    await state.clear()
    await _finish_screen(callback, texts.SLOT_SKIPPED)


# ─────────────────────────── Повтор замера (med_remeasure) ───────────────────────────


@router.callback_query(F.data.startswith("rem:snooze:"))
async def cb_snooze(
    callback: CallbackQuery,
    session: AsyncSession,
    state: FSMContext,
    reminders: RemindersService,
) -> None:
    """«Через 30 мин»: перенести напоминание, расписание не ломается (§6.7).

    Владелец строки — внутренний `users.id`, а не Telegram id: сервис ищет
    строку по `reminders.user_id`.
    """
    reminder_id = _id_of(callback)
    if reminder_id <= 0:
        await callback.answer()
        return
    user = await measurements_service.get_or_create_user(session, callback.from_user.id)  # type: ignore[arg-type]
    await session.commit()  # транзакция закрыта до работы сервиса (своя сессия)
    await state.clear()
    moved = await reminders.snooze(user.id, reminder_id)
    if not moved:
        await callback.answer()
        return
    await _finish_screen(callback, texts.SLOT_REMAINING)


@router.callback_query(F.data.startswith("rem:off:"))
async def cb_remeasure_off(
    callback: CallbackQuery,
    session: AsyncSession,
    state: FSMContext,
    reminders: RemindersService,
) -> None:
    """«Больше не напоминать»: закрыть этот повтор замера (§6.7).

    Строка повтора (доставленная или отложенная через «Через 30 мин»)
    удаляется, job снимается; настройка «Повтор после лекарства» не меняется.
    """
    reminder_id = _id_of(callback)
    if reminder_id <= 0:
        await callback.answer()
        return
    user = await measurements_service.get_or_create_user(session, callback.from_user.id)  # type: ignore[arg-type]
    await session.commit()
    await state.clear()
    removed = await reminders.cancel_remeasure(user.id, reminder_id)
    if not removed:
        await callback.answer()
        return
    await _finish_screen(callback, texts.REMEASURE_OFF_DONE)
