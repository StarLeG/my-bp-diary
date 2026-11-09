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
from sqlalchemy.ext.asyncio import AsyncSession

from bot import texts
from bot.models import Measurement, User
from bot.services import csv_export
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


def summary_lines(summary: stats_service.StatsSummary) -> tuple[str, ...]:
    """Строки сводки — те же метрики, что в блоке «Сводка» ЛК (§16.2, §10.1)."""
    lines = [
        f"Записей: {summary.count}",
        f"Систолическое: в среднем {summary.sys_avg} ({summary.sys_min}–{summary.sys_max})",
        f"Диастолическое: в среднем {summary.dia_avg} ({summary.dia_min}–{summary.dia_max})",
    ]
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
) -> ReportData:
    """Данные отчёта за период; `start_day=None` — «всё время» (§10.1).

    «Всё время» — от даты первой записи до сегодня (локальные сутки
    пациента). Нет записей — `ReportEmptyError`; больше `limit` (по
    умолчанию `REPORT_LIMIT`) — `ReportTooLargeError`.
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
    return ReportData(
        subject=subject,
        start_day=start_day,
        end_day=end_day,
        generated_at=generated_at,
        summary_lines=summary_lines(summary),
        rows=report_rows(measurements, cells, user.timezone),
        file_stem=file_stem,
    )


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
    _draw_legend(pdf)
    _draw_table(pdf, data.rows)
    return bytes(pdf.output())


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
