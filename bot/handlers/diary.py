"""Хендлер «Дневник за сегодня» (SPEC.md, §6.4).

Список измерений текущих локальных суток пользователя с итогом
«Всего записей: N»; пусто — «Сегодня записей ещё нет» + «Записать давление».
Сообщение редактируется (`edit_message_text`), FSM не трогается: просмотр
дневника не отменяет начатый ввод.
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery
from sqlalchemy.ext.asyncio import AsyncSession

from bot import keyboards, texts
from bot.models import Measurement
from bot.services import measurements as measurements_service

router = Router(name="diary")


def _line(measurement: Measurement, tz_name: str) -> str:
    """Строка дневника: локальное время + показатели и флаги (§6.4)."""
    local = measurement.timestamp.astimezone(ZoneInfo(tz_name))
    return texts.fmt_diary_line(
        local.strftime("%H:%M"),
        measurement.systolic,
        measurement.diastolic,
        measurement.pulse,
        measurement.medication_taken,
        measurement.feeling,
    )


@router.callback_query(F.data == "menu:diary")
async def cb_diary(callback: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    """«Дневник за сегодня» — список за локальные сутки (сообщение редактируется)."""
    tg_id = callback.from_user.id
    user = await measurements_service.get_or_create_user(session, tg_id)
    today = datetime.now(ZoneInfo(user.timezone)).date()
    rows = await measurements_service.measurements_for_day(session, user, today)
    if not rows:
        text, markup = texts.DIARY_EMPTY, keyboards.diary_kb(is_empty=True)
    else:
        lines = [_line(row, user.timezone) for row in rows]
        lines.append(texts.DIARY_TOTAL.format(count=len(rows)))
        text, markup = "\n".join(lines), keyboards.diary_kb()
    await callback.message.edit_text(text, reply_markup=markup)  # type: ignore[union-attr]
    await callback.answer()
