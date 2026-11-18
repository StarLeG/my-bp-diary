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
(7 / 30 / всё время / «📆 Свой период») → три файла документами: измерения,
приёмы лекарств и события (`csv_export`, лимит «всё время» — 5000 записей).
Сообщение выбора периода редактируется, файлы отправляются новыми сообщениями.

«Экспорт PDF» → период (7 / 30 / всё время / «📆 Свой период») → один
PDF-отчёт (`pdf_report`): на время генерации сообщение выбора периода
становится «Готовлю отчёт за ДД.ММ–ДД.ММ…» (для датированных периодов),
после отправки файла — снова экраном выбора периода. Пока отчёт пользователя
готовится, повторные нажатия `pdf:*` новую выгрузку не запускают (§7.2).

Свой период в выгрузках (2026-11-18, `doc/EXPORT-PERIOD-QUESTIONS.md`,
R1–R4) — тот же календарь §6.5, но без лимита длины (только 5000 записей);
после выбора дат — сразу генерация, ошибки — «📆 Изменить период» / «Назад».
Календарь параметризуется сценарием (`CalendarTarget`): область callback'ов,
лимит длины и действие по выбранным датам.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardMarkup, Message
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot import keyboards, texts
from bot.config import get_settings
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
CSV_KIND_EVENTS = "bp_events"


# ─────────────────────────── Экраны ───────────────────────────


async def _show(
    callback: CallbackQuery,
    text: str,
    markup: InlineKeyboardMarkup,
    *,
    parse_mode: Optional[str] = None,
) -> None:
    """Показать текстовый экран вместо текущего сообщения (§6.5).

    Текстовое сообщение редактируется; фото-гистограмма текстом стать не
    может — она удаляется, экран приходит новым сообщением. `parse_mode`
    передаётся точечно: только для сообщений с тегами (§11).
    """
    message = callback.message
    if message.photo:  # type: ignore[union-attr]
        await _delete(callback)
        await message.answer(text, reply_markup=markup, parse_mode=parse_mode)  # type: ignore[union-attr]
    else:
        try:
            await message.edit_text(text, reply_markup=markup, parse_mode=parse_mode)  # type: ignore[union-attr]
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


def _periods_screen(ui_large: bool = False) -> tuple[str, object]:
    return texts.BTN_STATS, keyboards.stats_period_kb(ui_large)


def _format_summary(
    days: int,
    start_day: date,
    end_day: date,
    user: User,
    summary: stats_service.StatsSummary,
    *,
    custom: bool = False,
    large: bool = False,
) -> str:
    """Сводка по числам сервиса; строка приверженности — только при лекарствах.

    `custom` — свой период: заголовок «За период … (N дней)» (§6.5, 0.5.3);
    `large` — крупный режим: значения жирным (§11).
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
        large=large,
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


@router.message(Command("stats"))
async def cmd_stats(message: Message, state: FSMContext, session: AsyncSession) -> None:
    """/stats — экран выбора периода (§6.5, §6.9, 0.7.0, MU13).

    Обёртка над экраном «Статистика»: FSM сбрасывается (канон §6.9), экран —
    новым сообщением.
    """
    await state.clear()
    user = await measurements_service.get_or_create_user(session, message.from_user.id)
    text, markup = _periods_screen(user.ui_large)
    await message.answer(text, reply_markup=markup)  # type: ignore[arg-type]


@router.callback_query(F.data.in_({"menu:stats", "stats:periods"}))
async def cb_stats_periods(callback: CallbackQuery, session: AsyncSession) -> None:
    """«Статистика» / «Назад» — выбор периода (под фото — новым сообщением)."""
    user = await measurements_service.get_or_create_user(session, callback.from_user.id)
    text, markup = _periods_screen(user.ui_large)
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
            await _broken(callback, large=user.ui_large)
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
        await _show(callback, texts.STATS_EMPTY, keyboards.stats_period_kb(user.ui_large))
        await callback.answer()
        return
    days = (end_day - start_day).days + 1
    text = _format_summary(days, start_day, end_day, user, summary, custom=custom, large=user.ui_large)
    footer = await _support_footer(session, user)
    if footer is not None:
        text = f"{text}\n\n{footer}"
    measurements = await measurements_service.measurements_for_period(
        session, user, start_day, end_day
    )
    chart_days = stats_chart.daily_values(measurements, user.timezone, start_day, end_day)
    await session.commit()  # фиксация last_support_shown_at (§7.2 — вне транзакций чтения)
    await callback.answer()
    png = await asyncio.to_thread(stats_chart.render, chart_days, large=user.ui_large)
    await _delete(callback)
    period = _period_key(start_day, end_day) if custom else str(days)
    await callback.message.answer_photo(  # type: ignore[union-attr]
        BufferedInputFile(png, filename=f"bp_stats_{start_day:%Y%m%d}-{end_day:%Y%m%d}.png"),
        caption=text,
        reply_markup=keyboards.stats_result_kb(period),
        # Теги `<b>` в подписи — только в крупном режиме (§11).
        parse_mode="HTML" if user.ui_large else None,
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


def _int(raw: str) -> Optional[int]:
    """Целое из callback_data; мусор — None (§8)."""
    return int(raw) if raw.isdigit() else None


def _parse_month(value: str) -> Optional[tuple[int, int]]:
    """`ГГГГММ` → (год, месяц); иначе None."""
    if len(value) != 6 or not value.isdigit():
        return None
    year, month = int(value[:4]), int(value[4:])
    if year < 1 or not 1 <= month <= 12:
        return None
    return year, month


def _check_period(
    first: date, last: date, today: date, max_days: Optional[int] = MAX_CUSTOM_DAYS
) -> tuple[Optional[tuple[date, date]], Optional[str]]:
    """Проверки своего периода (§6.5): (период, None) или (None, текст ошибки).

    Обратный порядок дат — меняются местами; будущее и длина сверх `max_days` —
    ошибка. `max_days=None` — лимита длины нет (выгрузки, R3).
    """
    if first > today or last > today:
        return None, texts.STATS_CAL_FUTURE
    start, end = min(first, last), max(first, last)
    if max_days is not None and (end - start).days + 1 > max_days:
        return None, texts.STATS_CAL_TOO_LONG
    return (start, end), None


@dataclass(frozen=True)
class CalendarTarget:
    """Сценарий календаря своего периода (§6.5, 2026-11-18, R2).

    `area` — область callback'ов (`stats` / `pdf` / `csv`), `max_days` — лимит
    длины периода (`None` — без ограничения, выгрузки — R3), `back` — callback
    кнопки «Назад» (экран «За какой период» своего сценария).
    """

    area: str
    max_days: Optional[int]
    back: str


#: Сценарии календаря: статистика и выгрузки (R1–R4).
TARGETS: dict[str, CalendarTarget] = {
    "stats": CalendarTarget("stats", MAX_CUSTOM_DAYS, "stats:periods"),
    "pdf": CalendarTarget("pdf", None, "stats:pdf"),
    "csv": CalendarTarget("csv", None, "stats:csv"),
}


def _target_of(data: Optional[str]) -> Optional[CalendarTarget]:
    """Сценарий по префиксу callback_data (`pdf:day:…` → «pdf»); мусор — None."""
    area = (data or "").split(":", 1)[0]
    return TARGETS.get(area)


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


async def _broken(
    callback: CallbackQuery, target: Optional[CalendarTarget] = None, *, large: bool = False
) -> None:
    """Битая или устаревшая callback_data — экран «За какой период» сценария (§6.5).

    Без `target` — экран статистики (как прежде).
    """
    target = target or TARGETS["stats"]
    if target.area == "stats":
        text, markup = _periods_screen(large)
    elif target.area == "pdf":
        text, markup = texts.CSV_ASK, keyboards.pdf_period_kb()
    else:
        text, markup = texts.CSV_ASK, keyboards.csv_period_kb()
    await _show(callback, text, markup)  # type: ignore[arg-type]
    await callback.answer()


async def _show_calendar(
    callback: CallbackQuery,
    year: int,
    month: int,
    today: date,
    start: Optional[date],
    *,
    large: bool = False,
    week_start: Optional[date] = None,
    target: Optional[CalendarTarget] = None,
) -> None:
    """Экран календаря: обычный — сетка месяца, крупный — список недели (UI12).

    `week_start` — понедельник показываемой недели (крупный режим, UI12);
    без него неделя выводится из `month` в `stats_calendar_kb`. `target` —
    сценарий (область callback'ов и «Назад»); без него — статистика (§6.5).
    """
    target = target or TARGETS["stats"]
    text = (
        texts.STATS_CAL_LAST.format(date=start.strftime("%d.%m"))
        if start is not None
        else texts.STATS_CAL_FIRST
    )
    await _show(
        callback,
        text,
        keyboards.stats_calendar_kb(
            year, month, today, start, large, week_start,
            area=target.area, back=target.back,
        ),
    )
    await callback.answer()


@router.callback_query(F.data.in_({"stats:noop", "pdf:noop", "csv:noop"}))
async def cb_stats_noop(callback: CallbackQuery) -> None:
    """Заглушки календаря (месяц, дни недели, пустые клетки) всех областей (§6.5)."""
    await callback.answer()


async def _handle_calendar(callback: CallbackQuery, session: AsyncSession) -> None:
    """«📆 Свой период» и листание: `{area}:cal[:m:{ГГГГММ}[:{с}]|w:{пн}[:{с}]]`.

    Обычный режим — сетка месяца `:cal:m:`, крупный — список недели `:cal:w:`
    (пн — понедельник недели, `с` — выбранный первый день шага 2). Прежние
    `stats:cal:{ГГГГММ}[:{с}]` принимаются как `:m:`-вариант (UI19). Сценарий
    (лимит длины, «Назад», действие по датам) — из области callback'ов (R2).
    """
    user = await measurements_service.get_or_create_user(session, callback.from_user.id)
    today = _today(user)
    large = user.ui_large
    target = _target_of(callback.data) or TARGETS["stats"]
    raw = (callback.data or "").split(":")[2:]
    kind = raw[0] if raw and raw[0] in ("m", "w") else "m"
    rest = raw[1:] if raw and raw[0] in ("m", "w") else raw
    if not raw:
        # «Свой период»: обычный — текущий месяц, крупный — неделя сегодняшнего (UI12).
        week_start = today - timedelta(days=today.weekday()) if large else None
        await _show_calendar(
            callback, today.year, today.month, today, None,
            large=large, week_start=week_start, target=target,
        )
        return
    if kind == "w":
        week_start = _parse_day(rest[0]) if len(rest) in (1, 2) else None
        start = _parse_day(rest[1]) if len(rest) == 2 else None
        if week_start is None or (len(rest) == 2 and (start is None or start > today)):
            await _broken(callback, target, large=user.ui_large)
            return
        await _show_calendar(
            callback,
            week_start.year,
            week_start.month,
            today,
            start,
            large=large,
            week_start=week_start,
            target=target,
        )
        return
    month = _parse_month(rest[0]) if len(rest) in (1, 2) else None
    start = _parse_day(rest[1]) if month is not None and len(rest) == 2 else None
    if (
        month is None
        or month > (today.year, today.month)
        or (len(rest) == 2 and (start is None or start > today))
    ):
        await _broken(callback, target, large=user.ui_large)
        return
    await _show_calendar(callback, month[0], month[1], today, start, large=large, target=target)


@router.callback_query((F.data == "stats:cal") | F.data.startswith("stats:cal:"))
async def cb_stats_calendar(callback: CallbackQuery, session: AsyncSession) -> None:
    """Календарь статистики: `stats:cal[:m:{ГГГГММ}[:{с}]|w:{пн}[:{с}]]` (UI12, UI19)."""
    await _handle_calendar(callback, session)


@router.callback_query(
    F.data.in_({"pdf:custom", "csv:custom", "pdf:cal", "csv:cal"})
    | F.data.startswith(("pdf:cal:", "csv:cal:"))
)
async def cb_export_calendar(callback: CallbackQuery, session: AsyncSession) -> None:
    """Календарь выгрузок (2026-11-18, R1/R2): `pdf:custom` / `csv:custom` и листание.

    Вход «📆 Свой период» в вопросе периода выгрузки — тот же календарь §6.5,
    но своей области: без лимита длины (R3), «Назад» — на вопрос периода.
    """
    await _handle_calendar(callback, session)


async def _finish_period(
    callback: CallbackQuery,
    session: AsyncSession,
    user: User,
    target: CalendarTarget,
    start: date,
    end: date,
) -> None:
    """Период выбран: сводка или выгрузка — «куда вернуться с датами» (R2/R4)."""
    if target.area == "pdf":
        await _export_pdf(callback, session, user, start, end, custom=True)
    elif target.area == "csv":
        await _export_csv(callback, session, user, start, end, custom=True)
    else:
        await _send_summary(callback, session, user, start, end, custom=True)


async def _handle_day(callback: CallbackQuery, session: AsyncSession) -> None:
    """Выбор дня: `{area}:day:{д}` — первый, `{area}:day:{с}:{д}` — последний (§6.5).

    Ошибки проверки — всплывающим уведомлением, календарь остаётся на месте.
    """
    user = await measurements_service.get_or_create_user(session, callback.from_user.id)
    today = _today(user)
    target = _target_of(callback.data) or TARGETS["stats"]
    parts = (callback.data or "").split(":")[2:]
    picked = [_parse_day(part) for part in parts]
    if not 1 <= len(picked) <= 2 or any(day is None for day in picked):
        await _broken(callback, target, large=user.ui_large)
        return
    if len(picked) == 1:
        first = picked[0]
        assert first is not None  # noqa: S101 — проверено выше
        if first > today:
            await callback.answer(texts.STATS_CAL_FUTURE)
            return
        # Шаг 2: выбранный день остаётся отмеченным «✓» на своей неделе (UI12).
        week_start = first - timedelta(days=first.weekday()) if user.ui_large else None
        await _show_calendar(
            callback,
            first.year,
            first.month,
            today,
            first,
            large=user.ui_large,
            week_start=week_start,
            target=target,
        )
        return
    first, last = picked
    assert first is not None and last is not None  # noqa: S101 — проверено выше
    period, error = _check_period(first, last, today, target.max_days)
    if period is None:
        await callback.answer(error)
        return
    await _finish_period(callback, session, user, target, period[0], period[1])


@router.callback_query(F.data.startswith("stats:day:"))
async def cb_stats_day(callback: CallbackQuery, session: AsyncSession) -> None:
    """Выбор дня статистики: `stats:day:{д}` / `stats:day:{с}:{д}` (§6.5)."""
    await _handle_day(callback, session)


@router.callback_query(F.data.startswith(("pdf:day:", "csv:day:")))
async def cb_export_day(callback: CallbackQuery, session: AsyncSession) -> None:
    """Выбор дня выгрузки (R1/R2): `pdf:day:…` / `csv:day:…` → выгрузка за период.

    Лимита длины нет (R3): годовой период берётся, проверяется только будущее
    и лимит 5000 записей (§10, §10.1).
    """
    await _handle_day(callback, session)


@router.callback_query(F.data.startswith("stats:days:"))
async def cb_stats_days(callback: CallbackQuery, session: AsyncSession) -> None:
    """«📅 По дням»: средние по дням с измерениями и итог «Среднее за день» (§6.5).

    `stats:days:{7|30}[:{limit}]` и `stats:days:{с}:{по}[:{limit}]` (§8); `limit`
    несёт «Показать ещё» крупного режима (по 5, UI13) — показаны первые `limit`
    строк. Обычный режим — весь список без пагинации.
    """
    user = await measurements_service.get_or_create_user(session, callback.from_user.id)
    today = _today(user)
    key = (callback.data or "").removeprefix("stats:days:")
    parts = key.split(":")
    limit: Optional[int] = None
    if parts[0] in ("7", "30") and len(parts) in (1, 2):
        period = parts[0]
        start_day, end_day = today - timedelta(days=int(period) - 1), today
        if len(parts) == 2:
            limit = _int(parts[1])
            if limit is None:
                await _broken(callback, large=user.ui_large)
                return
    elif len(parts) in (2, 3):
        period = f"{parts[0]}:{parts[1]}"
        period_range = _parse_period(period, today)
        if period_range is None or (len(parts) == 3 and (limit := _int(parts[2])) is None):
            await _broken(callback, large=user.ui_large)
            return
        start_day, end_day = period_range
    else:
        await _broken(callback, large=user.ui_large)
        return
    measurements = await measurements_service.measurements_for_period(
        session, user, start_day, end_day
    )
    averages = stats_service.daily_averages(measurements, user.timezone, start_day, end_day)
    await session.commit()  # сессия не держится во время отправки (§7.2)
    measured = averages.measured_days
    if not measured:
        await _show(callback, texts.STATS_EMPTY, keyboards.stats_period_kb(user.ui_large))
        await callback.answer()
        return
    rows = [
        (d.day.strftime("%d.%m"), d.zone, d.systolic, d.diastolic, d.pulse)
        for d in measured
    ]
    shown = limit if user.ui_large else None  # обычный режим — весь список (UI13)
    total = len(rows)
    rows = rows[:shown] if shown is not None else rows
    text = texts.fmt_daily_averages(
        start_day.strftime("%d.%m"),
        end_day.strftime("%d.%m"),
        rows,  # type: ignore[arg-type]
        (averages.systolic, averages.diastolic, averages.pulse),  # type: ignore[arg-type]
        large=user.ui_large,
    )
    await _show(
        callback,
        text,
        keyboards.stats_days_kb(
            period,
            large=user.ui_large,
            total=total,
            limit=shown if shown is not None else total,
        ),
        parse_mode="HTML",
    )
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
    """Даты для имени файла «всё время» — фактический диапазон записей.

    Измерений нет — диапазон по событиям (§10, 0.6.2); и того, и другого
    нет — сегодняшний день.
    """
    from bot.models import DiaryEvent

    row = (
        await session.execute(
            select(func.min(Measurement.timestamp), func.max(Measurement.timestamp)).where(
                Measurement.user_id == user.id
            )
        )
    ).one()
    if row[0] is None:
        ev = (
            await session.execute(
                select(func.min(DiaryEvent.moment), func.max(DiaryEvent.moment)).where(
                    DiaryEvent.user_id == user.id
                )
            )
        ).one()
        if ev[0] is None:
            return today, today
        row = ev
    tz = ZoneInfo(user.timezone)
    return row[0].astimezone(tz).date(), row[1].astimezone(tz).date()


def _csv_is_empty(payload: bytes, kind: str, tz_name: str) -> bool:
    """CSV без строк данных — только шапка (пустой период, §10, R4).

    Сверка с пустым рендером того же вида: точнее, чем считать строки, —
    поля с переводами строк разрывают подсчёт.
    """
    builder = {
        CSV_KIND_MEASUREMENTS: csv_export.build_measurements_csv,
        CSV_KIND_MEDS: csv_export.build_meds_csv,
        CSV_KIND_EVENTS: csv_export.build_events_csv,
    }[kind]
    return payload == builder([], tz_name)


async def _export_csv(
    callback: CallbackQuery,
    session: AsyncSession,
    user: User,
    start: Optional[date],
    end: Optional[date],
    *,
    custom: bool,
) -> None:
    """Выгрузка: три CSV документами (измерения, приёмы, события — §10).

    `start=None` — «всё время» (лимит 5000). `custom` — свой период (R3/R4):
    ошибки лимита и пустого периода — «📆 Изменить период» / «Назад» вместо
    обрыва сценария; пустой период — сообщение, файлы не отправляются.
    """
    retry = keyboards.export_retry_kb("csv") if custom else keyboards.csv_period_kb()
    today = datetime.now(ZoneInfo(user.timezone)).date()
    if start is None or end is None:
        limit: Optional[int] = csv_export.ALL_TIME_LIMIT
        range_start, range_end = await _all_time_range(session, user, today)
    else:
        # Свой период ограничен только лимитом 5000 записей (R3); 7/30 — как прежде.
        limit = csv_export.ALL_TIME_LIMIT if custom else None
        range_start, range_end = start, end
    content = await csv_export.export_measurements(session, user, start, end, limit=limit)
    meds = await csv_export.export_medications(session, user, start, end, limit=limit)
    events = await csv_export.export_events(session, user, start, end, limit=limit)
    if content is None or meds is None or events is None:
        await callback.message.edit_text(  # type: ignore[union-attr]
            texts.CSV_TOO_MANY, reply_markup=retry
        )
        await callback.answer()
        return
    if all(
        _csv_is_empty(payload, kind, user.timezone)
        for kind, payload in (
            (CSV_KIND_MEASUREMENTS, content),
            (CSV_KIND_MEDS, meds),
            (CSV_KIND_EVENTS, events),
        )
    ):
        await callback.message.edit_text(  # type: ignore[union-attr]
            texts.PDF_EMPTY, reply_markup=retry
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
    await callback.message.answer_document(  # type: ignore[union-attr]
        BufferedInputFile(
            events, filename=csv_export.file_name(CSV_KIND_EVENTS, range_start, range_end)
        ),
        caption=texts.CSV_FILE_CAPTION,
    )
    await callback.answer()


@router.callback_query(F.data.in_({"csv:7", "csv:30", "csv:all"}))
async def cb_csv_export(callback: CallbackQuery, session: AsyncSession) -> None:
    """Выгрузка CSV за фиксированный период (§10)."""
    period = callback.data.split(":", 1)[1]  # type: ignore[union-attr]
    user = await measurements_service.get_or_create_user(session, callback.from_user.id)
    today = datetime.now(ZoneInfo(user.timezone)).date()
    start: Optional[date] = None
    end: Optional[date] = None
    if period != "all":
        end = today
        start = today - timedelta(days=int(period) - 1)
    await _export_csv(callback, session, user, start, end, custom=False)


# ─────────────────────────── Экспорт PDF ───────────────────────────

#: Пользователи, чей PDF-отчёт сейчас готовится: двойное нажатие — одна выгрузка.
_pdf_in_progress: set[int] = set()


@router.callback_query(F.data == "stats:pdf")
async def cb_pdf_menu(callback: CallbackQuery) -> None:
    """«Экспорт PDF» — выбор периода отчёта (§10.1); под фото — новым сообщением."""
    await _show(callback, texts.CSV_ASK, keyboards.pdf_period_kb())
    await callback.answer()


async def _export_pdf(
    callback: CallbackQuery,
    session: AsyncSession,
    user: User,
    start: Optional[date],
    end: Optional[date],
    *,
    custom: bool,
) -> None:
    """Выгрузка PDF-отчёта документом (§10.1).

    `start=None` — «всё время». Данные читаются и транзакция закрывается до
    генерации и отправки файла; PDF рисуется в thread pool (§7.2 п.7). Пустой
    период и лимит — текст вместо файла; `custom` — свой период (R3/R4): на
    время генерации «Готовлю отчёт за ДД.ММ–ДД.ММ…», ошибки — «📆 Изменить
    период» / «Назад».
    """
    tg_id = callback.from_user.id
    if tg_id in _pdf_in_progress:
        await callback.answer(texts.PDF_PREPARING)
        return
    _pdf_in_progress.add(tg_id)
    try:
        await callback.answer()
        if start is not None and end is not None:
            preparing = texts.PDF_PREPARING_PERIOD.format(
                date_from=f"{start:%d.%m}", date_to=f"{end:%d.%m}"
            )
        else:
            preparing = texts.PDF_PREPARING
        await callback.message.edit_text(preparing)  # type: ignore[union-attr]
        problem: Optional[str] = None
        try:
            data = await pdf_report.collect_report(
                session, user, start, end, subject=pdf_report.patient_subject(user),
                include_conclusions=True,
                qr_landing_url=get_settings().landing_url,
                qr_bot_url=get_settings().bot_url,
            )
        except pdf_report.ReportEmptyError:
            problem = texts.PDF_EMPTY
        except pdf_report.ReportTooLargeError:
            problem = texts.CSV_TOO_MANY
        await session.commit()  # транзакция закрыта до генерации и отправки
        if problem is not None:
            await callback.message.edit_text(  # type: ignore[union-attr]
                problem,
                reply_markup=(
                    keyboards.export_retry_kb("pdf") if custom else keyboards.pdf_period_kb()
                ),
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


@router.callback_query(F.data.in_({"pdf:7", "pdf:30", "pdf:all"}))
async def cb_pdf_export(callback: CallbackQuery, session: AsyncSession) -> None:
    """Выгрузка PDF-отчёта за фиксированный период (§10.1)."""
    period = callback.data.split(":", 1)[1]  # type: ignore[union-attr]
    user = await measurements_service.get_or_create_user(session, callback.from_user.id)
    start: Optional[date] = None
    end: Optional[date] = None
    if period != "all":
        end = datetime.now(ZoneInfo(user.timezone)).date()
        start = end - timedelta(days=int(period) - 1)
    await _export_pdf(callback, session, user, start, end, custom=False)
