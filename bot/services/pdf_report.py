"""PDF-отчёт по давлению — один формат для бота, ЛК и кабинета врача (SPEC.md, §10.1).

Отчёт повторяет «Историю записей» личного кабинета (§16.2): шапка, сводка
(та же `stats.summary`, что в ЛК), легенда зон и таблица — Дата, Время,
Сист., Диаст., Пульс, Лекарство, Самочувствие, Заметка. Строки подсвечены
цветом худшей из зон САД/ДАД (только жёлтая и красная), пороги и цвета —
из `web/cabinet/chart_zones.py`; полупрозрачная заливка кабинета переводится
в непрозрачный цвет на белом фоне (так строка выглядит и в браузере).

Работа в два шага (§7.2 п.7):
1. `collect_report` (async) читает БД и собирает `ReportData` — только
   значения, без объектов ORM;
2. `build_pdf` (sync, вызывается через `asyncio.to_thread`) рисует файл.

Пустой период и превышение лимита — исключения `ReportEmptyError` и
`ReportTooLargeError`: файл в этих случаях не формируется.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Optional, Sequence
from zoneinfo import ZoneInfo

from fpdf import FPDF
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot import texts
from bot.models import Appointment, DiaryEvent, Measurement, MeasurementMedication, MedicationLog, User
from bot.services import appointments as appointments_service
from bot.services import csv_export
from bot.services import events as events_service
from bot.services import measurements as measurements_service
from bot.services import medications as medications_service
from bot.services import stats as stats_service
from web.cabinet.chart_zones import CHART_ZONES, band_index, table_legend

#: Каталог встроенных шрифтов: DejaVuSans покрывает кириллицу (свободная лицензия).
FONTS_DIR = Path(__file__).resolve().parent / "fonts"
#: Обычное начертание.
FONT_REGULAR_PATH = FONTS_DIR / "DejaVuSans.ttf"
#: Жирное начертание: заголовок, шапка таблицы.
FONT_BOLD_PATH = FONTS_DIR / "DejaVuSans-Bold.ttf"
#: Имя семейства шрифта внутри PDF.
FONT_FAMILY = "DejaVu"

#: Лимит записей за период — как у CSV (§10, §10.1).
REPORT_LIMIT = csv_export.ALL_TIME_LIMIT

#: Область имени файла: `bp_diary_<дата1>-<дата2>.pdf` (§10.1).
FILE_KIND = "bp_diary"

#: Заголовок отчёта.
REPORT_TITLE = "Отчёт по давлению"
#: Заголовок раздела заключений в PDF пациента (0.5.11, В32-Г, Р6).
CONCLUSIONS_TITLE = "Заключения врачей"
#: Подпись пациента в отчёте самого пациента (бот и ЛК): без telegram_id —
#: файл могут переслать (§10.1).
PATIENT_SUBJECT = "Дневник давления"

#: Заголовки колонок — как в «Истории записей» ЛК (§16.2), без «Действий».
COLUMNS = (
    "Дата",
    "Время",
    "Сист.",
    "Диаст.",
    "Пульс",
    "Лекарство",
    "Самочувствие",
    "Заметка",
)
#: Ширины колонок, мм: сумма — полезная ширина A4 альбомной (297 − 2 × 10).
COLUMN_WIDTHS = (21, 14, 14, 16, 14, 66, 30, 102)

#: Поля страницы, мм.
PAGE_MARGIN = 10
#: Нижний отступ автопереноса страницы: под колонтитулом «Стр. N из M».
BOTTOM_MARGIN = 15

#: Кегль таблицы, пт.
TABLE_FONT_SIZE = 9
#: Высота строки текста в ячейке, мм.
LINE_HEIGHT = 4.5
#: Внутренний отступ ячейки, мм.
CELL_PADDING = 1.2

#: Фон шапки таблицы (мягкий голубой, как заголовки таблиц кабинета).
HEADINGS_FILL = (232, 240, 250)

_RGBA_RE = re.compile(
    r"rgba\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*([0-9]*\.?[0-9]+)\s*\)"
)

#: Высота графика сводки, мм (0.7.2, R10-А: сводка + график + легенда укладываются
#: в ~105 мм — таблица гарантированно начинается на первой странице).
CHART_HEIGHT = 50
#: Маркеры точек — до этого числа записей (0.7.2, R11-А); одна запись — только маркер.
CHART_MARK_LIMIT = 30
#: С этого числа точек — прореживание (0.7.2, R11-А).
CHART_DECIMATE_LIMIT = 500
#: Пороговые линии графика (0.7.2, R13-А): единообразие с зонами ЛК.
CHART_THRESHOLD_SYS = 140
CHART_THRESHOLD_DIA = 90
#: Цвета рядов графика.
CHART_COLOR_SYS = (200, 70, 70)
CHART_COLOR_DIA = (70, 110, 200)
#: Заголовок раздела «События дневника» (§10.1, 0.7.2, R9-Б).
DIARY_TITLE = "События дневника"


@dataclass(frozen=True)
class ChartPoint:
    """Точка графика сводки (0.7.2, R3-А); `moment` — UTC (как `measurements.timestamp`)."""

    moment: datetime
    systolic: int
    diastolic: int


class ReportEmptyError(Exception):
    """За период нет записей — файл не формируется (§10.1)."""


class ReportTooLargeError(Exception):
    """Записей за период больше лимита — файл не формируется (§10.1)."""


@dataclass(frozen=True)
class ReportRow:
    """Строка таблицы отчёта — готовые значения ячеек.

    `zone` — номер худшей полосы САД/ДАД (1 — жёлтая, 2 — красная);
    None — обе в норме, строка без подсветки.
    """

    date: str
    time: str
    systolic: str
    diastolic: str
    pulse: str
    medication: str
    feeling: str
    note: str
    zone: Optional[int] = None

    def cells(self) -> tuple[str, ...]:
        """Значения ячеек в порядке `COLUMNS`."""
        return (
            self.date,
            self.time,
            self.systolic,
            self.diastolic,
            self.pulse,
            self.medication,
            self.feeling,
            self.note,
        )


@dataclass(frozen=True)
class ConclusionGroup:
    """Группа раздела «Заключения врачей»: заголовок события и тексты (Р7).

    Фото (`photo_file_id`) и заметка события не включаются (§10.1).
    """

    header: str
    items: tuple[str, ...]


@dataclass(frozen=True)
class ReportData:
    """Всё, что нужно `build_pdf`: значения, без объектов ORM (§7.2 п.7)."""

    subject: str
    start_day: date
    end_day: date
    generated_at: datetime
    summary_lines: tuple[str, ...]
    rows: tuple[ReportRow, ...]
    file_stem: Optional[str] = None
    """Начало имени файла врача: `Ivanov_I_I` (§10.1, 0.5.1); None — `bp_diary`."""
    conclusions: tuple[ConclusionGroup, ...] = ()
    """«Заключения врачей» — только в PDF пациента (У2-А); у врача пусто."""
    chart_points: tuple[ChartPoint, ...] = ()
    """Точки графика САД/ДАД после сводки (0.7.2, R3-А); пусто — графика нет."""
    diary_lines: tuple[str, ...] = ()
    """Строки раздела «События дневника» (0.7.2, R9-Б); пусто — раздела нет."""

    @property
    def file_name(self) -> str:
        """Имя файла `bp_diary_<дата1>-<дата2>.pdf` (та же функция, что у CSV).

        У врача при заполненных данных пациента — `Ivanov_I_I_<дата1>-<дата2>.pdf`.
        """
        return csv_export.file_name(
            self.file_stem or FILE_KIND, self.start_day, self.end_day, ext="pdf"
        )


def doctor_subject(telegram_id: int) -> str:
    """Подпись пациента в отчёте врача: у врача несколько пациентов (§10.1)."""
    return f"Пациент {telegram_id}"


def doctor_subject_for(user: User) -> str:
    """Подпись в отчёте врача (§10.1, 0.5.1): ФИО и дата рождения; без данных —
    имя из Telegram или «Пациент {telegram_id}»."""
    from bot.services import profile as profile_service

    return profile_service.report_subject(user) or profile_service.fallback_name(user)


def patient_subject(user: User) -> str:
    """Подпись в отчёте самого пациента: ФИО и дата рождения или «Дневник давления»."""
    from bot.services import profile as profile_service

    return profile_service.report_subject(user) or PATIENT_SUBJECT


# ─────────────────────────── Значения ───────────────────────────


def row_zone_level(systolic: int, diastolic: int) -> Optional[int]:
    """Худшая полоса САД/ДАД: 1 — жёлтая, 2 — красная, None — норма (§16.2)."""
    worst = max(
        band_index(systolic, CHART_ZONES["systolic"]),
        band_index(diastolic, CHART_ZONES["diastolic"]),
    )
    return worst or None


def opaque_fill(rgba: str) -> tuple[int, int, int]:
    """Полупрозрачный цвет кабинета `rgba(r,g,b,a)` → непрозрачный на белом фоне.

    Канал = 255 − (255 − c) × a — тот же цвет, что видит браузер на белом.
    """
    match = _RGBA_RE.fullmatch(rgba.strip())
    if match is None:
        raise ValueError(f"не rgba-цвет: {rgba!r}")
    red, green, blue = (int(match.group(i)) for i in (1, 2, 3))
    alpha = float(match.group(4))
    return tuple(round(255 - (255 - channel) * alpha) for channel in (red, green, blue))  # type: ignore[return-value]


def zone_fill(level: int) -> tuple[int, int, int]:
    """Непрозрачная заливка строки для полосы `level` (цвета — `CHART_ZONES`)."""
    return opaque_fill(CHART_ZONES["systolic"][level]["fill"])


def summary_lines(summary: stats_service.StatsSummary, tz_name: str) -> tuple[str, ...]:
    """Строки сводки — те же метрики, что в блоке «Сводка» ЛК (§16.2, §10.1).

    `tz_name` — часовой пояс пациента: моменты записей-экстремумов (0.7.2, R1-А)
    показываются в локальном времени.
    """
    lines = [
        f"Записей: {summary.count}",
        f"Систолическое: в среднем {summary.sys_avg} ({summary.sys_min}–{summary.sys_max})",
        f"Диастолическое: в среднем {summary.dia_avg} ({summary.dia_min}–{summary.dia_max})",
    ]
    if summary.max_entry is not None:
        lines.append(f"Максимальное: {summary.max_entry.text(tz_name)}")
    if summary.min_entry is not None:
        lines.append(f"Минимальное: {summary.min_entry.text(tz_name)}")
    if summary.pulse_avg is not None:
        lines.append(f"Пульс: в среднем {summary.pulse_avg}")
    lines.append(f"Тренд: {summary.trend}")
    if summary.goal_verdict:
        lines.append(f"Цель: {summary.goal_verdict}")
    if summary.adherence:
        taken, planned = summary.adherence
        lines.append(f"Лекарства: принято {taken} из {planned} приёмов")
    return tuple(lines)


def report_rows(
    measurements: Sequence[Measurement], medication_cells: dict[int, str], tz_name: str
) -> tuple[ReportRow, ...]:
    """Строки таблицы по записям — значения как в «Истории записей» ЛК (§16.2)."""
    tz = ZoneInfo(tz_name)
    rows: list[ReportRow] = []
    for m in measurements:
        moment = m.timestamp.astimezone(tz)
        rows.append(
            ReportRow(
                date=f"{moment:%d.%m.%Y}",
                time=f"{moment:%H:%M}",
                systolic=str(m.systolic),
                diastolic=str(m.diastolic),
                pulse=str(m.pulse) if m.pulse is not None else "—",
                medication=medication_cells[m.id],
                feeling=texts.FEELING_LABELS.get(m.feeling, "—") if m.feeling else "—",
                note=m.note or "",
                zone=row_zone_level(m.systolic, m.diastolic),
            )
        )
    return tuple(rows)


# ─────────────────────── Раздел «События дневника» (0.7.2) ───────────────────────


def diary_event_line(event: DiaryEvent, tz_name: str) -> str:
    """Строка события раздела: «ДД.ММ.ГГГГ ЧЧ:ММ — тип — текст» (§10.1, R9-Б).

    Текст — `summary` по словарю CSV событий (§10) плюс `note` полностью через
    «; » (для `stress` заметка в ленте ЛК скрыта, §6.4, но в PDF нужна врачу);
    у `note`-события `summary` пуст — текст только из заметки.
    """
    local = event.moment.astimezone(ZoneInfo(tz_name))
    title = texts.EVENT_TYPE_TITLES.get(event.type, texts.EVENT_TYPE_TITLES["note"])
    summary = csv_export.event_summary(event)
    note = (event.note or "").strip()
    text = "; ".join(part for part in (summary, note) if part)
    head = f"{local:%d.%m.%Y %H:%M} — {title}"
    return f"{head} — {text}" if text else head


def diary_intake_line(log: MedicationLog, name: str, dosage: str, tz_name: str) -> str:
    """Строка standalone-приёма: «ДД.ММ.ГГГГ ЧЧ:ММ — Приём лекарств: … — Принято».

    Статус — словами («Принято» / «Пропущено» / «Частично»), без ✓/✗/~ —
    как в колонке «Лекарство» таблицы (§10.1).
    """
    local = log.timestamp.astimezone(ZoneInfo(tz_name))
    item = texts.fmt_intake_item(name, dosage, log.slot)
    status = texts.PDF_INTAKE_STATUS.get(log.status, log.status)
    return f"{local:%d.%m.%Y %H:%M} — Приём лекарств: {item} — {status}"


async def _journal_pairs(
    session: AsyncSession, user: User, measurements: Sequence[Measurement]
) -> set[tuple[int, datetime]]:
    """Пары (лекарство, момент) журнала записей (§4.11) — для исключения дублей.

    Строка `medication_logs`, чья пара `medication_id + timestamp` совпадает со
    связью `measurement_medications` измерения этого момента, — журнал записи:
    в раздел не попадает (уже отражена в колонке «Лекарство», §10.1).
    """
    ids = [m.id for m in measurements]
    if not ids:
        return set()
    rows = await session.execute(
        select(MeasurementMedication.medication_id, Measurement.timestamp)
        .join(Measurement, Measurement.id == MeasurementMedication.measurement_id)
        .where(
            MeasurementMedication.measurement_id.in_(ids),
            Measurement.user_id == user.id,
        )
    )
    return {(medication_id, timestamp) for medication_id, timestamp in rows.all()}


async def diary_lines(
    session: AsyncSession,
    user: User,
    start_day: date,
    end_day: date,
    measurements: Sequence[Measurement],
) -> tuple[str, ...]:
    """Строки раздела «События дневника» (§10.1, 0.7.2, R8/R9): по времени вверх.

    События дневника и standalone-приёмы; журналы приёмов, привязанные к
    измерению, не дублируются (§4.11). Без измерений раздел не строится —
    PDF в этом случае не формируется вовсе (R14-А).
    """
    events = await events_service.events_for_period(session, user, start_day, end_day)
    logs = await medications_service.logs_for_period(session, user, start_day, end_day)
    journal = await _journal_pairs(session, user, measurements)
    entries: list[tuple[datetime, str]] = [
        (event.moment, diary_event_line(event, user.timezone)) for event in events
    ]
    for log, name, dosage in logs:
        if (log.medication_id, log.timestamp) in journal:
            continue
        entries.append(
            (log.timestamp, diary_intake_line(log, name, dosage, user.timezone))
        )
    entries.sort(key=lambda pair: pair[0])
    return tuple(line for _, line in entries)


async def collect_report(
    session: AsyncSession,
    user: User,
    start_day: Optional[date],
    end_day: Optional[date],
    *,
    subject: str,
    limit: Optional[int] = None,
    now: Optional[datetime] = None,
    file_stem: Optional[str] = None,
    include_conclusions: bool = False,
) -> ReportData:
    """Данные отчёта за период; `start_day=None` — «всё время» (§10.1).

    «Всё время» — от даты первой записи до сегодня (локальные сутки
    пациента). Нет записей — `ReportEmptyError`; больше `limit` (по
    умолчанию `REPORT_LIMIT`) — `ReportTooLargeError`. `include_conclusions` —
    раздел «Заключения врачей» (В32-Г): только у пациента (У2-А), у врача — False.
    """
    tz = ZoneInfo(user.timezone)
    generated_at = (now or measurements_service.utcnow()).astimezone(tz)
    if start_day is None or end_day is None:
        measurements = await measurements_service.measurements_all(session, user)
        if measurements:
            start_day = measurements[0].timestamp.astimezone(tz).date()
        end_day = generated_at.date()
    else:
        measurements = await measurements_service.measurements_for_period(
            session, user, start_day, end_day
        )
    if not measurements:
        raise ReportEmptyError
    if len(measurements) > (REPORT_LIMIT if limit is None else limit):
        raise ReportTooLargeError
    assert start_day is not None  # есть записи → есть дата первой
    summary = await stats_service.summary(session, user, start_day, end_day)
    cells = await medications_service.medication_cells(session, user, measurements)
    conclusions: tuple[ConclusionGroup, ...] = ()
    if include_conclusions:
        conclusions = await _conclusion_groups(session, user, start_day, end_day)
    return ReportData(
        subject=subject,
        start_day=start_day,
        end_day=end_day,
        generated_at=generated_at,
        summary_lines=summary_lines(summary, user.timezone),
        rows=report_rows(measurements, cells, user.timezone),
        file_stem=file_stem,
        conclusions=conclusions,
        chart_points=tuple(
            ChartPoint(m.timestamp, m.systolic, m.diastolic) for m in measurements
        ),
        diary_lines=await diary_lines(session, user, start_day, end_day, measurements),
    )


async def _conclusion_groups(
    session: AsyncSession, user: User, start_day: date, end_day: date
) -> tuple[ConclusionGroup, ...]:
    """«Заключения врачей» за период: группировка по событию (Р7), по дате события."""
    groups = []
    for appt, items in await appointments_service.conclusions_in_period(
        session, user, start_day, end_day
    ):
        groups.append(
            ConclusionGroup(
                # Иконки (§10.1: «{иконка} {title} — {ДД.ММ.ГГГГ}») в PDF нет:
                # DejaVuSans не содержит эмодзи — их место было бы пустым или «тофу».
                header=f"{appt.title} — {appt.starts_on:%d.%m.%Y}",
                items=tuple(item.text for item in items),
            )
        )
    return tuple(groups)


# ─────────────────────────── PDF ───────────────────────────


class _ReportPDF(FPDF):
    """A4 альбомная с колонтитулом «Стр. N из M» внизу каждой страницы."""

    def footer(self) -> None:
        """Номер страницы; `{nb}` fpdf2 заменяет общим числом страниц."""
        self.set_y(-PAGE_MARGIN)
        self.set_font(FONT_FAMILY, size=8)
        self.set_text_color(110, 110, 110)
        self.cell(0, 5, text=f"Стр. {self.page_no()} из {{nb}}", align="C")
        self.set_text_color(0, 0, 0)


def build_pdf(data: ReportData) -> bytes:
    """Нарисовать отчёт (синхронно — вызывать через `asyncio.to_thread`, §7.2 п.7)."""
    pdf = _ReportPDF(orientation="L", format="A4")
    pdf.set_margins(PAGE_MARGIN, PAGE_MARGIN, PAGE_MARGIN)
    pdf.set_auto_page_break(auto=True, margin=BOTTOM_MARGIN)
    pdf.add_font(FONT_FAMILY, "", str(FONT_REGULAR_PATH))
    pdf.add_font(FONT_FAMILY, "B", str(FONT_BOLD_PATH))
    pdf.set_title(REPORT_TITLE)
    pdf.add_page()

    _draw_header(pdf, data)
    _draw_summary(pdf, data.summary_lines)
    _draw_chart(pdf, data.chart_points)
    _draw_legend(pdf)
    _draw_table(pdf, data.rows)
    _draw_diary(pdf, data.diary_lines)
    _draw_conclusions(pdf, data.conclusions)
    return bytes(pdf.output())


# ─────────────────────────── График сводки (0.7.2) ───────────────────────────


def decimate_points(
    points: Sequence[ChartPoint], limit: int = CHART_DECIMATE_LIMIT
) -> tuple[ChartPoint, ...]:
    """Прореживание точек графика (R11-А); до `limit` — без изменений.

    При > 500 точках (экспорт «всё время») в каждом отрезке времени
    сохраняются точки с минимумом и максимумом САД и ДАД — экстремумы не
    теряются, линия остаётся (в отличие от «только маркеры», R11-Б).
    """
    pts = list(points)
    if len(pts) <= limit:
        return tuple(pts)
    buckets = limit // 4  # до 4 точек на отрезок: мин/макс САД и ДАД
    chosen = {0, len(pts) - 1}
    for i in range(buckets):
        lo = i * len(pts) // buckets
        hi = (i + 1) * len(pts) // buckets
        chunk = pts[lo:hi] or [pts[lo]]
        for attr in ("systolic", "diastolic"):
            chosen.add(lo + min(range(len(chunk)), key=lambda j: getattr(chunk[j], attr)))
            chosen.add(lo + max(range(len(chunk)), key=lambda j: getattr(chunk[j], attr)))
    return tuple(pts[i] for i in sorted(chosen))


def _draw_chart(pdf: FPDF, points: Sequence[ChartPoint]) -> None:
    """Линейный график САД/ДАД после сводки, до легенды зон (§10.1, R3-А).

    Ось X — по реальному времени (R12-А): пропуски между днями видны; ось Y —
    авто-шкала по данным с запасом. Пороги 140/90 — тонкий пунктир (R13-А).
    Одна запись — маркер без линии; до 30 точек — линия + маркеры; больше —
    линия (R11-А). Без пульса — другая область значений (R3-А).
    """
    if not points:
        return
    pts = list(decimate_points(points))
    left = pdf.l_margin
    width = pdf.w - pdf.l_margin - pdf.r_margin
    top = pdf.get_y()
    plot_h = CHART_HEIGHT - 8  # запас под подписи рядов и ось X
    plot_top = top + 5

    values = [p.systolic for p in pts] + [p.diastolic for p in pts]
    v_min = min(min(values), CHART_THRESHOLD_DIA)
    v_max = max(max(values), CHART_THRESHOLD_SYS)
    pad = max(5, round((v_max - v_min) * 0.05))
    v_min -= pad
    v_max += pad
    t_min = min(p.moment for p in pts)
    t_max = max(p.moment for p in pts)
    span = (t_max - t_min).total_seconds()

    def x_at(moment: datetime) -> float:
        if span <= 0:
            return left + width / 2
        return left + (moment - t_min).total_seconds() / span * width

    def y_at(value: int) -> float:
        return plot_top + plot_h * (v_max - value) / (v_max - v_min)

    # Пороги 140/90 — тонкий пунктир с подписями у оси Y (R13-А).
    pdf.set_draw_color(170, 170, 170)
    pdf.set_line_width(0.2)
    pdf.set_font(FONT_FAMILY, size=7)
    pdf.set_dash_pattern(dash=1.5, gap=1.5)
    for value in (CHART_THRESHOLD_SYS, CHART_THRESHOLD_DIA):
        y = y_at(value)
        pdf.line(left, y, left + width, y)
        pdf.set_text_color(110, 110, 110)
        pdf.text(left + 2, y - 1, text=str(value))
    pdf.set_dash_pattern()  # сплошные линии для рядов

    # Ряды: линия, маркеры — до 30 точек (R11-А).
    single = len(pts) == 1
    mark = single or len(pts) <= CHART_MARK_LIMIT
    for attr, color in (("systolic", CHART_COLOR_SYS), ("diastolic", CHART_COLOR_DIA)):
        pdf.set_draw_color(*color)
        pdf.set_fill_color(*color)
        pdf.set_line_width(0.4)
        if not single:
            for a, b in zip(pts, pts[1:]):
                pdf.line(
                    x_at(a.moment), y_at(getattr(a, attr)),
                    x_at(b.moment), y_at(getattr(b, attr)),
                )
        if mark:
            for p in pts:
                pdf.circle(x_at(p.moment), y_at(getattr(p, attr)), 0.6, style="F")

    # Подписи рядов (R3-А) — над графиком, слева.
    pdf.set_font(FONT_FAMILY, "B", 8)
    pdf.set_text_color(*CHART_COLOR_SYS)
    pdf.text(left, top + 3, text="САД")
    pdf.set_text_color(*CHART_COLOR_DIA)
    pdf.text(left + 12, top + 3, text="ДАД")
    pdf.set_text_color(0, 0, 0)
    pdf.set_xy(left, top + CHART_HEIGHT + 1)


def _draw_diary(pdf: FPDF, lines: Sequence[str]) -> None:
    """Раздел «События дневника» после таблицы (§10.1, 0.7.2, R9-Б).

    Заголовок — как у «Заключений врачей»; строки — по одной, длинный текст
    переносится по словам (правило таблицы).
    """
    if not lines:
        return
    pdf.ln(4)
    pdf.set_font(FONT_FAMILY, "B", 12)
    pdf.set_x(pdf.l_margin)
    pdf.multi_cell(0, 7, text=DIARY_TITLE)
    pdf.set_font(FONT_FAMILY, "", 10)
    for line in lines:
        pdf.set_x(pdf.l_margin)
        pdf.multi_cell(0, 5.5, text=line)


def _draw_conclusions(pdf: FPDF, groups: Sequence[ConclusionGroup]) -> None:
    """«Заключения врачей» после таблицы, в конце документа (Р6, У2-А)."""
    if not groups:
        return
    pdf.ln(4)
    pdf.set_font(FONT_FAMILY, "B", 12)
    pdf.set_x(pdf.l_margin)
    pdf.multi_cell(0, 7, text=CONCLUSIONS_TITLE)
    for group in groups:
        pdf.ln(1)
        pdf.set_font(FONT_FAMILY, "B", 10)
        pdf.set_x(pdf.l_margin)
        pdf.multi_cell(0, 6, text=group.header)
        pdf.set_font(FONT_FAMILY, "", 10)
        if len(group.items) == 1:
            numbered = (group.items[0],)
        else:
            numbered = tuple(f"{index}. {item}" for index, item in enumerate(group.items, 1))
        for item in numbered:
            pdf.set_x(pdf.l_margin)
            for line in _wrap(pdf, item, pdf.epw):
                pdf.set_x(pdf.l_margin)
                pdf.multi_cell(0, 5, text=line)
        pdf.ln(1)


def _draw_header(pdf: FPDF, data: ReportData) -> None:
    """Заголовок, период и подпись пациента со временем формирования."""
    pdf.set_font(FONT_FAMILY, "B", 15)
    pdf.cell(0, 8, text=REPORT_TITLE, new_x="LMARGIN", new_y="NEXT")
    pdf.set_font(FONT_FAMILY, size=10)
    pdf.cell(
        0,
        6,
        text=f"Период: {data.start_day:%d.%m.%Y} — {data.end_day:%d.%m.%Y}",
        new_x="LMARGIN",
        new_y="NEXT",
    )
    pdf.cell(
        0,
        6,
        text=f"{data.subject} · сформирован {data.generated_at:%d.%m.%Y %H:%M}",
        new_x="LMARGIN",
        new_y="NEXT",
    )
    pdf.ln(2)


def _draw_summary(pdf: FPDF, lines: Sequence[str]) -> None:
    """Сводка: подзаголовок и строки метрик."""
    pdf.set_font(FONT_FAMILY, "B", 11)
    pdf.cell(0, 7, text="Сводка", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font(FONT_FAMILY, size=10)
    for line in lines:
        pdf.cell(0, 5.5, text=line, new_x="LMARGIN", new_y="NEXT")
    pdf.ln(3)


def _draw_legend(pdf: FPDF) -> None:
    """Легенда зон над таблицей — две плашки, как в ЛК (§16.2)."""
    pdf.set_font(FONT_FAMILY, size=9)
    for level, item in enumerate(table_legend(), start=1):
        pdf.set_fill_color(*zone_fill(level))
        pdf.set_draw_color(200, 200, 200)
        width = pdf.get_string_width(item["label"]) + 6
        pdf.cell(width, 6, text=item["label"], border=1, fill=True, align="C")
        pdf.cell(3, 6)
    pdf.ln(9)
    pdf.set_draw_color(0, 0, 0)
    # Цвет заливки — состояние документа: без сброса строки таблицы без зоны
    # унаследовали бы цвет последней плашки легенды.
    pdf.set_fill_color(255, 255, 255)


def _wrap(pdf: FPDF, text: str, width: float) -> list[str]:
    """Строки ячейки шириной `width`: короткий текст — без расчёта переноса.

    Перенос по словам (`multi_cell` в режиме dry_run) считается только для
    текста шире колонки — таких ячеек в отчёте немного, а расчёт дорогой.
    """
    if not text:
        return [""]
    if "\n" not in text and pdf.get_string_width(text) <= width:
        return [text]
    return pdf.multi_cell(width, LINE_HEIGHT, text, dry_run=True, output="LINES")


def _draw_row(
    pdf: FPDF,
    cells: Sequence[str],
    fill: Optional[tuple[int, int, int]],
    *,
    bold: bool = False,
) -> None:
    """Строка таблицы: рамки ячеек, заливка и текст по центру по вертикали.

    Перед отрисовкой проверяется место: строка целиком уходит на новую
    страницу, где первой повторяется шапка таблицы (§10.1).
    """
    pdf.set_font(FONT_FAMILY, "B" if bold else "", TABLE_FONT_SIZE)
    lines = [
        _wrap(pdf, text, width - 2 * CELL_PADDING)
        for text, width in zip(cells, COLUMN_WIDTHS)
    ]
    height = max(len(cell_lines) for cell_lines in lines) * LINE_HEIGHT + 2 * CELL_PADDING
    if not bold and pdf.get_y() + height > pdf.page_break_trigger:
        pdf.add_page()
        _draw_row(pdf, COLUMNS, HEADINGS_FILL, bold=True)
        pdf.set_font(FONT_FAMILY, size=TABLE_FONT_SIZE)
    top = pdf.get_y()
    x = pdf.l_margin
    if fill is not None:
        pdf.set_fill_color(*fill)
    # Базовая линия текста — как у `cell`: середина строки + 0.3 кегля.
    baseline = LINE_HEIGHT / 2 + 0.3 * pdf.font_size
    for cell_lines, width in zip(lines, COLUMN_WIDTHS):
        pdf.rect(x, top, width, height, style="DF" if fill is not None else "D")
        text_top = top + (height - len(cell_lines) * LINE_HEIGHT) / 2
        for index, line in enumerate(cell_lines):
            if line:
                pdf.text(x + CELL_PADDING, text_top + index * LINE_HEIGHT + baseline, line)
        x += width
    pdf.set_xy(pdf.l_margin, top + height)


def _draw_table(pdf: FPDF, rows: Sequence[ReportRow]) -> None:
    """Таблица записей: перенос по словам, шапка повторяется на каждой странице.

    Рисуется вручную, а не `FPDF.table`: та дважды переносит текст каждой
    ячейки и на 20 одновременных выгрузках врача давала многократное
    замедление (`qa/test_load_doctor.py`). Строка, не помещающаяся на
    странице, целиком уходит на следующую.
    """
    pdf.set_draw_color(190, 190, 190)
    pdf.set_text_color(0, 0, 0)
    fills = {level: zone_fill(level) for level in (1, 2)}
    _draw_row(pdf, COLUMNS, HEADINGS_FILL, bold=True)
    for row in rows:
        _draw_row(pdf, row.cells(), fills[row.zone] if row.zone else None)
