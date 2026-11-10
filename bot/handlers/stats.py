"""Хендлеры статистики, экспорта CSV и PDF (SPEC.md, §6.5, §10 и §10.1).

Период кнопками «7 дней» / «30 дней» / «📆 Свой период» (инлайн-календарь без
FSM, ≤ 31 дня) → фото-гистограмма (`stats_chart`) с
текстовой сводкой (`fmt_stats`) в подписи + футер «Поддержать» (≥ 3 записей и
не чаще раза в сутки, при показе `users.last_support_shown_at` обновляется).
Текстовое сообщение выбора периода в фото не превращается (ограничение
Telegram) — оно удаляется и отправляется фото; кнопки под фото удаляют его
и показывают свой экран новым текстовым сообщением (§6.5). «📅 По дням» — список
средних по дням с измерениями и итог «Среднее за день» (`stats.daily_averages`).
«Экспорт CSV» → период
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
        try:
            await message.edit_text(text, reply_markup=markup)  # type: ignore[union-attr]
        except TelegramBadRequest as error:
            # Двойное нажатие той же кнопки календаря — тот же экран (§7.2).
            if "message is not modified" not in str(error):
                raise


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
    *,
    custom: bool = False,
) -> str:
    """Сводка по числам сервиса; строка приверженности — только при лекарствах.

    `custom` — свой период: заголовок «За период … (N дней)» (§6.5, 0.5.3).
    """
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
        custom=custom,
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


@router.callback_query(F.data.in_({"stats:7", "stats:30"}) | F.data.startswith("stats:c:"))
async def cb_stats_show(callback: CallbackQuery, session: AsyncSession) -> None:
    """Сводка за 7/30 суток или свой период: фото-гистограмма со сводкой (§6.5).

    `stats:c:{с}:{по}` — свой период («Назад» из «По дням»); битые или
    недопустимые даты — экран выбора периода.
    """
    data = callback.data or ""
    user = await measurements_service.get_or_create_user(session, callback.from_user.id)
    today = _today(user)
    if data.startswith("stats:c:"):
        period = _parse_period(data.removeprefix("stats:c:"), today)
        if period is None:
            await _broken(callback)
            return
        await _send_summary(callback, session, user, period[0], period[1], custom=True)
        return
    days = int(data.split(":")[1])
    await _send_summary(
        callback, session, user, today - timedelta(days=days - 1), today, custom=False
    )


async def _send_summary(
    callback: CallbackQuery,
    session: AsyncSession,
    user: User,
    start_day: date,
    end_day: date,
    *,
    custom: bool,
) -> None:
    """Фото-гистограмма со сводкой в подписи; нет записей — текст (§6.5).

    Картинка рисуется в потоке (§7.2 п.7) по значениям дней; транзакция
    закрыта до генерации и отправки.
    """
    summary = await stats_service.summary(session, user, start_day, end_day)
    if summary.count == 0:
        await session.commit()
        await _show(callback, texts.STATS_EMPTY, keyboards.stats_period_kb())
        await callback.answer()
        return
    days = (end_day - start_day).days + 1
    text = _format_summary(days, start_day, end_day, user, summary, custom=custom)
    footer = await _support_footer(session, user)
    if footer is not None:
        text = f"{text}\n\n{footer}"
    measurements = await measurements_service.measurements_for_period(
        session, user, start_day, end_day
    )
    chart_days = stats_chart.daily_values(measurements, user.timezone, start_day, end_day)
    await session.commit()  # фиксация last_support_shown_at (§7.2 — вне транзакций чтения)
    await callback.answer()
    png = await asyncio.to_thread(stats_chart.render, chart_days)
    await _delete(callback)
    period = _period_key(start_day, end_day) if custom else str(days)
    await callback.message.answer_photo(  # type: ignore[union-attr]
        BufferedInputFile(png, filename=f"bp_stats_{start_day:%Y%m%d}-{end_day:%Y%m%d}.png"),
        caption=text,
        reply_markup=keyboards.stats_result_kb(period),
    )


# ─────────────────────────── Свой период (0.5.3) ───────────────────────────

#: Наибольшая длина своего периода в боте, дней (§6.5).
MAX_CUSTOM_DAYS = 31


def _today(user: User) -> date:
    return datetime.now(ZoneInfo(user.timezone)).date()


def _parse_day(value: str) -> Optional[date]:
    """`ГГГГММДД` → дата; иначе None."""
    if len(value) != 8 or not value.isdigit():
        return None
    try:
        return datetime.strptime(value, "%Y%m%d").date()
    except ValueError:
        return None


def _parse_month(value: str) -> Optional[tuple[int, int]]:
    """`ГГГГММ` → (год, месяц); иначе None."""
    if len(value) != 6 or not value.isdigit():
        return None
    year, month = int(value[:4]), int(value[4:])
    if year < 1 or not 1 <= month <= 12:
        return None
    return year, month


def _check_period(
    first: date, last: date, today: date
) -> tuple[Optional[tuple[date, date]], Optional[str]]:
    """Проверки своего периода (§6.5): (период, None) или (None, текст ошибки).

    Обратный порядок дат — меняются местами; будущее и > 31 дня — ошибка.
    """
    if first > today or last > today:
        return None, texts.STATS_CAL_FUTURE
    start, end = min(first, last), max(first, last)
    if (end - start).days + 1 > MAX_CUSTOM_DAYS:
        return None, texts.STATS_CAL_TOO_LONG
    return (start, end), None


def _parse_period(value: str, today: date) -> Optional[tuple[date, date]]:
    """`ГГГГММДД:ГГГГММДД` из callback_data → допустимый период или None."""
    parts = value.split(":")
    if len(parts) != 2:
        return None
    first, last = _parse_day(parts[0]), _parse_day(parts[1])
    if first is None or last is None:
        return None
    period, _ = _check_period(first, last, today)
    return period


def _period_key(start_day: date, end_day: date) -> str:
    return f"{start_day:%Y%m%d}:{end_day:%Y%m%d}"


async def _broken(callback: CallbackQuery) -> None:
    """Битая или устаревшая callback_data — экран выбора периода (§6.5)."""
    text, markup = _periods_screen()
    await _show(callback, text, markup)  # type: ignore[arg-type]
    await callback.answer()


async def _show_calendar(
    callback: CallbackQuery, year: int, month: int, today: date, start: Optional[date]
) -> None:
    text = (
        texts.STATS_CAL_LAST.format(date=start.strftime("%d.%m"))
        if start is not None
        else texts.STATS_CAL_FIRST
    )
    await _show(callback, text, keyboards.stats_calendar_kb(year, month, today, start))
    await callback.answer()


@router.callback_query(F.data == "stats:noop")
async def cb_stats_noop(callback: CallbackQuery) -> None:
    """Заглушки календаря (месяц, дни недели, пустые клетки)."""
    await callback.answer()


@router.callback_query((F.data == "stats:cal") | F.data.startswith("stats:cal:"))
async def cb_stats_calendar(callback: CallbackQuery, session: AsyncSession) -> None:
    """«📆 Свой период» и листание месяцев: `stats:cal[:{ГГГГММ}[:{с}]]` (§6.5, §8)."""
    user = await measurements_service.get_or_create_user(session, callback.from_user.id)
    today = _today(user)
    parts = (callback.data or "").split(":")[2:]
    if not parts:
        await _show_calendar(callback, today.year, today.month, today, None)
        return
    month = _parse_month(parts[0]) if len(parts) <= 2 else None
    start = _parse_day(parts[1]) if month is not None and len(parts) == 2 else None
    if (
        month is None
        or month > (today.year, today.month)
        or (len(parts) == 2 and (start is None or start > today))
    ):
        await _broken(callback)
        return
    await _show_calendar(callback, month[0], month[1], today, start)


@router.callback_query(F.data.startswith("stats:day:"))
async def cb_stats_day(callback: CallbackQuery, session: AsyncSession) -> None:
    """Выбор дня: `stats:day:{д}` — первый, `stats:day:{с}:{д}` — последний (§6.5).

    Ошибки проверки — всплывающим уведомлением, календарь остаётся на месте.
    """
    user = await measurements_service.get_or_create_user(session, callback.from_user.id)
    today = _today(user)
    parts = (callback.data or "").split(":")[2:]
    picked = [_parse_day(part) for part in parts]
    if not 1 <= len(picked) <= 2 or any(day is None for day in picked):
        await _broken(callback)
        return
    if len(picked) == 1:
        first = picked[0]
        assert first is not None  # noqa: S101 — проверено выше
        if first > today:
            await callback.answer(texts.STATS_CAL_FUTURE)
            return
        await _show_calendar(callback, first.year, first.month, today, first)
        return
    first, last = picked
    assert first is not None and last is not None  # noqa: S101 — проверено выше
    period, error = _check_period(first, last, today)
    if period is None:
        await callback.answer(error)
        return
    await _send_summary(callback, session, user, period[0], period[1], custom=True)


@router.callback_query(F.data.startswith("stats:days:"))
async def cb_stats_days(callback: CallbackQuery, session: AsyncSession) -> None:
    """«📅 По дням»: средние по дням с измерениями и итог «Среднее за день» (§6.5)."""
    user = await measurements_service.get_or_create_user(session, callback.from_user.id)
    today = _today(user)
    key = (callback.data or "").removeprefix("stats:days:")
    if key in ("7", "30"):
        start_day, end_day = today - timedelta(days=int(key) - 1), today
    else:
        period = _parse_period(key, today)
        if period is None:
            await _broken(callback)
            return
        start_day, end_day = period
    measurements = await measurements_service.measurements_for_period(
        session, user, start_day, end_day
    )
    averages = stats_service.daily_averages(measurements, user.timezone, start_day, end_day)
    await session.commit()  # сессия не держится во время отправки (§7.2)
    measured = averages.measured_days
    if not measured:
        await _show(callback, texts.STATS_EMPTY, keyboards.stats_period_kb())
        await callback.answer()
        return
    rows = [
        (d.day.strftime("%d.%m"), d.zone, d.systolic, d.diastolic, d.pulse)
        for d in measured
    ]
    text = texts.fmt_daily_averages(
        start_day.strftime("%d.%m"),
        end_day.strftime("%d.%m"),
        rows,  # type: ignore[arg-type]
        (averages.systolic, averages.diastolic, averages.pulse),  # type: ignore[arg-type]
    )
    await _show(callback, text, keyboards.stats_days_kb(key))
    await callback.answer()


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
