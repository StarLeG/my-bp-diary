"""Хендлеры статистики и экспорта CSV (SPEC.md, §6.5 и §10).

Период кнопками «7 дней» / «30 дней» → текстовая сводка (`fmt_stats`) +
футер «Поддержать» (≥ 3 записей и не чаще раза в сутки, при показе
`users.last_support_shown_at` обновляется). «Экспорт CSV» → период
(7 / 30 / всё время) → два файла документами: измерения и приёмы лекарств
(`csv_export`, лимит «всё время» — 5000 записей). Сообщение выбора периода
редактируется, файлы отправляются новыми сообщениями.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.types import BufferedInputFile, CallbackQuery
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot import keyboards, texts
from bot.models import Measurement, User
from bot.services import csv_export
from bot.services import measurements as measurements_service
from bot.services import stats as stats_service

router = Router(name="stats")

#: Минимум записей для футера «Поддержать» (§6.5).
SUPPORT_MIN_MEASUREMENTS = 3

#: Пауза между показами футера «Поддержать» (§6.5: «не чаще раза в сутки»).
SUPPORT_FOOTER_INTERVAL = timedelta(hours=24)

#: Область файлов экспорта (имена: `<область>_<дата1>-<дата2>.csv`, §10).
CSV_KIND_MEASUREMENTS = "bp_diary"
CSV_KIND_MEDS = "bp_meds"


# ─────────────────────────── Экраны ───────────────────────────


def _periods_screen() -> tuple[str, object]:
    return texts.BTN_STATS, keyboards.stats_period_kb()


def _format_summary(
    days: int,
    start_day: date,
    end_day: date,
    user: User,
    summary: stats_service.StatsSummary,
) -> str:
    """Сводка по числам сервиса; строка приверженности — только при лекарствах."""
    adherence = None
    if summary.adherence is not None:
        taken, planned = summary.adherence
        adherence = texts.STATS_ADHERENCE.format(taken=taken, planned=planned)
    return texts.fmt_stats(
        days=days,
        date_from=start_day.strftime("%d.%m"),
        date_to=end_day.strftime("%d.%m"),
        count=summary.count,
        sys_avg=summary.sys_avg,  # type: ignore[arg-type]
        sys_min=summary.sys_min,  # type: ignore[arg-type]
        sys_max=summary.sys_max,  # type: ignore[arg-type]
        dia_avg=summary.dia_avg,  # type: ignore[arg-type]
        dia_min=summary.dia_min,  # type: ignore[arg-type]
        dia_max=summary.dia_max,  # type: ignore[arg-type]
        pulse_avg=summary.pulse_avg,
        trend=summary.trend,
        target=f"{user.target_systolic}/{user.target_diastolic}",
        goal_verdict=summary.goal_verdict or texts.STATS_GOAL_OK,
        adherence=adherence,
    )


async def _support_footer(session: AsyncSession, user: User) -> Optional[str]:
    """Футер «Поддержать»: ≥ 3 записей и не чаще раза в сутки (§6.5)."""
    if (
        await measurements_service.count_measurements(session, user)
        < SUPPORT_MIN_MEASUREMENTS
    ):
        return None
    now = measurements_service.utcnow()
    shown = user.last_support_shown_at
    if shown is not None:
        if shown.tzinfo is None:
            shown = shown.replace(tzinfo=now.tzinfo)
        if now - shown < SUPPORT_FOOTER_INTERVAL:
            return None
    user.last_support_shown_at = now
    return texts.STATS_FOOTER_SUPPORT


# ─────────────────────────── Статистика ───────────────────────────


@router.callback_query(F.data.in_({"menu:stats", "stats:periods"}))
async def cb_stats_periods(callback: CallbackQuery) -> None:
    """«Статистика» / «Назад» — выбор периода (сообщение редактируется)."""
    text, markup = _periods_screen()
    await callback.message.edit_text(text, reply_markup=markup)  # type: ignore[union-attr]
    await callback.answer()


@router.callback_query(F.data.in_({"stats:7", "stats:30"}))
async def cb_stats_show(callback: CallbackQuery, session: AsyncSession) -> None:
    """Сводка за 7/30 локальных суток; футер «Поддержать» — по правилам §6.5."""
    days = int(callback.data.split(":")[1])  # type: ignore[union-attr]
    tg_id = callback.from_user.id
    user = await measurements_service.get_or_create_user(session, tg_id)
    today = datetime.now(ZoneInfo(user.timezone)).date()
    start_day = today - timedelta(days=days - 1)
    summary = await stats_service.summary(session, user, start_day, today)
    if summary.count == 0:
        text, markup = texts.STATS_EMPTY, keyboards.stats_period_kb()
    else:
        text = _format_summary(days, start_day, today, user, summary)
        footer = await _support_footer(session, user)
        if footer is not None:
            text = f"{text}\n\n{footer}"
        markup = keyboards.stats_result_kb()
    await session.commit()  # фиксация last_support_shown_at (§7.2 — вне транзакций чтения)
    await callback.message.edit_text(text, reply_markup=markup)  # type: ignore[union-attr]
    await callback.answer()


# ─────────────────────────── Экспорт CSV ───────────────────────────


@router.callback_query(F.data == "stats:csv")
async def cb_csv_menu(callback: CallbackQuery) -> None:
    """«Экспорт CSV» — выбор периода выгрузки (§10)."""
    await callback.message.edit_text(  # type: ignore[union-attr]
        texts.CSV_ASK, reply_markup=keyboards.csv_period_kb()
    )
    await callback.answer()


async def _all_time_range(
    session: AsyncSession, user: User, today: date
) -> tuple[date, date]:
    """Даты для имени файла «всё время» — фактический диапазон записей."""
    row = (
        await session.execute(
            select(func.min(Measurement.timestamp), func.max(Measurement.timestamp)).where(
                Measurement.user_id == user.id
            )
        )
    ).one()
    if row[0] is None:
        return today, today
    tz = ZoneInfo(user.timezone)
    return row[0].astimezone(tz).date(), row[1].astimezone(tz).date()


@router.callback_query(F.data.in_({"csv:7", "csv:30", "csv:all"}))
async def cb_csv_export(callback: CallbackQuery, session: AsyncSession) -> None:
    """Выгрузка: два CSV документами (измерения и приёмы лекарств, §10)."""
    period = callback.data.split(":", 1)[1]  # type: ignore[union-attr]
    tg_id = callback.from_user.id
    user = await measurements_service.get_or_create_user(session, tg_id)
    today = datetime.now(ZoneInfo(user.timezone)).date()
    if period == "all":
        start: Optional[date] = None
        end: Optional[date] = None
        limit: Optional[int] = csv_export.ALL_TIME_LIMIT
        range_start, range_end = await _all_time_range(session, user, today)
    else:
        days = int(period)
        range_start = today - timedelta(days=days - 1)
        range_end = today
        start, end, limit = range_start, range_end, None
    content = await csv_export.export_measurements(session, user, start, end, limit=limit)
    meds = await csv_export.export_medications(session, user, start, end, limit=limit)
    if content is None or meds is None:
        await callback.message.edit_text(  # type: ignore[union-attr]
            texts.CSV_TOO_MANY, reply_markup=keyboards.csv_period_kb()
        )
        await callback.answer()
        return
    await callback.message.answer_document(  # type: ignore[union-attr]
        BufferedInputFile(
            content, filename=csv_export.file_name(CSV_KIND_MEASUREMENTS, range_start, range_end)
        ),
        caption=texts.CSV_FILE_CAPTION,
    )
    await callback.message.answer_document(  # type: ignore[union-attr]
        BufferedInputFile(
            meds, filename=csv_export.file_name(CSV_KIND_MEDS, range_start, range_end)
        ),
        caption=texts.CSV_FILE_CAPTION,
    )
    await callback.answer()
