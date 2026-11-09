"""Хендлеры статистики, экспорта CSV и PDF (SPEC.md, §6.5, §10 и §10.1).

Период кнопками «7 дней» / «30 дней» → фото-гистограмма (`stats_chart`) с
текстовой сводкой (`fmt_stats`) в подписи + футер «Поддержать» (≥ 3 записей и
не чаще раза в сутки, при показе `users.last_support_shown_at` обновляется).
Текстовое сообщение выбора периода в фото не превращается (ограничение
Telegram) — оно удаляется и отправляется фото; кнопки под фото удаляют его
и показывают свой экран новым текстовым сообщением (§6.5). «Экспорт CSV» → период
(7 / 30 / всё время) → два файла документами: измерения и приёмы лекарств
(`csv_export`, лимит «всё время» — 5000 записей). Сообщение выбора периода
редактируется, файлы отправляются новыми сообщениями.

«Экспорт PDF» → период (7 / 30 / всё время) → один PDF-отчёт (`pdf_report`):
на время генерации сообщение выбора периода становится «Готовлю отчёт…»,
после отправки файла — снова экраном выбора периода. Пока отчёт пользователя
готовится, повторные нажатия `pdf:*` новую выгрузку не запускают (§7.2).
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardMarkup
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot import keyboards, texts
from bot.models import Measurement, User
from bot.services import csv_export
from bot.services import pdf_report
from bot.services import measurements as measurements_service
from bot.services import stats as stats_service
from bot.services import stats_chart

router = Router(name="stats")

#: Минимум записей для футера «Поддержать» (§6.5).
SUPPORT_MIN_MEASUREMENTS = 3

#: Пауза между показами футера «Поддержать» (§6.5: «не чаще раза в сутки»).
SUPPORT_FOOTER_INTERVAL = timedelta(hours=24)

#: Область файлов экспорта (имена: `<область>_<дата1>-<дата2>.csv`, §10).
CSV_KIND_MEASUREMENTS = "bp_diary"
CSV_KIND_MEDS = "bp_meds"


# ─────────────────────────── Экраны ───────────────────────────


async def _show(callback: CallbackQuery, text: str, markup: InlineKeyboardMarkup) -> None:
    """Показать текстовый экран вместо текущего сообщения (§6.5).

    Текстовое сообщение редактируется; фото-гистограмма текстом стать не
    может — она удаляется, экран приходит новым сообщением.
    """
    message = callback.message
    if message.photo:  # type: ignore[union-attr]
        await _delete(callback)
        await message.answer(text, reply_markup=markup)  # type: ignore[union-attr]
    else:
        await message.edit_text(text, reply_markup=markup)  # type: ignore[union-attr]


async def _delete(callback: CallbackQuery) -> None:
    """Удалить сообщение с кнопкой; уже удалённое или старше 48 ч — не ошибка."""
    try:
        await callback.message.delete()  # type: ignore[union-attr]
    except TelegramBadRequest:
        pass


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
    """«Статистика» / «Назад» — выбор периода (под фото — новым сообщением)."""
    text, markup = _periods_screen()
    await _show(callback, text, markup)  # type: ignore[arg-type]
    await callback.answer()


@router.callback_query(F.data.in_({"stats:7", "stats:30"}))
async def cb_stats_show(callback: CallbackQuery, session: AsyncSession) -> None:
    """Сводка за 7/30 локальных суток: фото-гистограмма со сводкой в подписи (§6.5).

    Нет записей — прежний текст без картинки. Картинка рисуется в потоке
    (§7.2 п.7) по значениям дней; транзакция закрыта до генерации и отправки.
    """
    days = int(callback.data.split(":")[1])  # type: ignore[union-attr]
    tg_id = callback.from_user.id
    user = await measurements_service.get_or_create_user(session, tg_id)
    today = datetime.now(ZoneInfo(user.timezone)).date()
    start_day = today - timedelta(days=days - 1)
    summary = await stats_service.summary(session, user, start_day, today)
    if summary.count == 0:
        await session.commit()
        await _show(callback, texts.STATS_EMPTY, keyboards.stats_period_kb())
        await callback.answer()
        return
    text = _format_summary(days, start_day, today, user, summary)
    footer = await _support_footer(session, user)
    if footer is not None:
        text = f"{text}\n\n{footer}"
    measurements = await measurements_service.measurements_for_period(
        session, user, start_day, today
    )
    chart_days = stats_chart.daily_values(measurements, user.timezone, start_day, today)
    await session.commit()  # фиксация last_support_shown_at (§7.2 — вне транзакций чтения)
    await callback.answer()
    png = await asyncio.to_thread(stats_chart.render, chart_days)
    await _delete(callback)
    await callback.message.answer_photo(  # type: ignore[union-attr]
        BufferedInputFile(png, filename=f"bp_stats_{start_day:%Y%m%d}-{today:%Y%m%d}.png"),
        caption=text,
        reply_markup=keyboards.stats_result_kb(),
    )


# ─────────────────────────── Экспорт CSV ───────────────────────────


@router.callback_query(F.data == "stats:csv")
async def cb_csv_menu(callback: CallbackQuery) -> None:
    """«Экспорт CSV» — выбор периода выгрузки (§10); под фото — новым сообщением."""
    await _show(callback, texts.CSV_ASK, keyboards.csv_period_kb())
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


# ─────────────────────────── Экспорт PDF ───────────────────────────

#: Пользователи, чей PDF-отчёт сейчас готовится: двойное нажатие — одна выгрузка.
_pdf_in_progress: set[int] = set()


@router.callback_query(F.data == "stats:pdf")
async def cb_pdf_menu(callback: CallbackQuery) -> None:
    """«Экспорт PDF» — выбор периода отчёта (§10.1); под фото — новым сообщением."""
    await _show(callback, texts.CSV_ASK, keyboards.pdf_period_kb())
    await callback.answer()


@router.callback_query(F.data.in_({"pdf:7", "pdf:30", "pdf:all"}))
async def cb_pdf_export(callback: CallbackQuery, session: AsyncSession) -> None:
    """Выгрузка PDF-отчёта документом (§10.1).

    Данные читаются и транзакция закрывается до генерации и отправки файла;
    PDF рисуется в thread pool (§7.2 п.7). Пустой период и лимит — текст
    вместо файла на том же экране выбора периода.
    """
    tg_id = callback.from_user.id
    if tg_id in _pdf_in_progress:
        await callback.answer(texts.PDF_PREPARING)
        return
    _pdf_in_progress.add(tg_id)
    try:
        await callback.answer()
        await callback.message.edit_text(texts.PDF_PREPARING)  # type: ignore[union-attr]
        period = callback.data.split(":", 1)[1]  # type: ignore[union-attr]
        user = await measurements_service.get_or_create_user(session, tg_id)
        start: Optional[date] = None
        end: Optional[date] = None
        if period != "all":
            end = datetime.now(ZoneInfo(user.timezone)).date()
            start = end - timedelta(days=int(period) - 1)
        problem: Optional[str] = None
        try:
            data = await pdf_report.collect_report(
                session, user, start, end, subject=pdf_report.patient_subject(user)
            )
        except pdf_report.ReportEmptyError:
            problem = texts.PDF_EMPTY
        except pdf_report.ReportTooLargeError:
            problem = texts.CSV_TOO_MANY
        await session.commit()  # транзакция закрыта до генерации и отправки
        if problem is not None:
            await callback.message.edit_text(  # type: ignore[union-attr]
                problem, reply_markup=keyboards.pdf_period_kb()
            )
            return
        payload = await asyncio.to_thread(pdf_report.build_pdf, data)
        await callback.message.answer_document(  # type: ignore[union-attr]
            BufferedInputFile(payload, filename=data.file_name),
            caption=texts.PDF_FILE_CAPTION.format(
                date_from=f"{data.start_day:%d.%m}", date_to=f"{data.end_day:%d.%m}"
            ),
        )
        await callback.message.edit_text(  # type: ignore[union-attr]
            texts.CSV_ASK, reply_markup=keyboards.pdf_period_kb()
        )
    finally:
        _pdf_in_progress.discard(tg_id)
