"""Гистограмма статистики бота: PNG (SPEC.md, §6.5, «Гистограмма»).

Столбик — один день периода: средние за локальные сутки САД (вверх от
средней линии), ДАД (вниз), пульс — серым числом на самой линии. Масштаб —
от базового уровня, а не от 0 (САД от 90, ДАД от 50), чтобы разница между
днями была видна; верх шкалы — max(180, наибольшее САД), низ — max(110,
наибольшее ДАД), с округлением вверх до 10. Каждая половина столбика —
цветом своей зоны (`web/cabinet/chart_zones.py`, базовый RGB без
прозрачности). День без измерений — пустое место.

Два шага (§7.2 п.7): `daily_values` превращает записи в значения по дням
(без ORM в потоке), `render` рисует PNG Pillow — вызывать через
`asyncio.to_thread`.
"""
from __future__ import annotations

import io
import math
import re
from dataclasses import dataclass
from datetime import date
from typing import Iterable, Optional, Sequence

from PIL import Image, ImageDraw, ImageFont

from bot.models import Measurement
from bot.services import stats as stats_service
from bot.services.pdf_report import FONT_REGULAR_PATH
from web.cabinet.chart_zones import CHART_ZONES, band_index

#: Размер картинки, px (§6.5).
WIDTH, HEIGHT = 1080, 720

#: Базовые уровни: САД растёт вверх от 90, ДАД — вниз от 50 (мм рт. ст.).
SYS_BASE, DIA_BASE = 90, 50
#: Минимальные границы шкалы; расширяются по данным с шагом 10.
SYS_MIN_TOP, DIA_MIN_BOTTOM = 180, 110
#: Шаг округления границ шкалы.
SCALE_STEP = 10

#: Кегль подписей, px. В чате Telegram картинка ужимается примерно до 300 px
#: по ширине (в 3,5 раза), поэтому шрифт крупный: иначе шкала, легенда,
#: пульс и даты не читаются без открытия картинки.
#:
#: Крупный режим (UI11): кегль +30% с округлением вверх до чётного —
#: `large` (значения 44, пульс 40/26, даты и шкала 38, легенда 36); при
#: переполнении (подпись значения шире столбика) — `large_tight`
#: (42/38/36/34). `pulse_long` — пульс при периоде больше 7 дней.
FONT_SIZES: dict[str, dict[str, int]] = {
    "normal": {
        "value": 34,
        "pulse": 30,
        "pulse_long": 20,
        "date": 28,
        "scale": 28,
        "legend": 26,
    },
    "large": {
        "value": 44,
        "pulse": 40,
        "pulse_long": 26,
        "date": 38,
        "scale": 38,
        "legend": 36,
    },
    "large_tight": {
        "value": 42,
        "pulse": 38,
        "pulse_long": 24,
        "date": 36,
        "scale": 36,
        "legend": 34,
    },
}

#: Кегль единицы измерения («мм рт. ст.») — не входит в пересчёт UI11.
FONT_UNIT = 24

#: Поля области графика, px (слева — под шкалу; с запасом: Telegram Web
#: подрезает края фото в пузыре).
LEFT, RIGHT, TOP = 112, 40, 48
#: Высота области под графиком: даты + легенда.
BOTTOM_AREA = 132
#: Полоса средней линии под числа пульса, px.
PULSE_BAND = 40
#: Доля ширины слота дня под столбик.
BAR_RATIO = 0.62
#: Подписи значений на концах столбиков — только для коротких периодов.
VALUE_LABELS_MAX_DAYS = 7
#: Запас над и под столбиками под подписи значений (короткий период), px.
VALUE_LABEL_ROOM = 44
#: Шаг подписи дат на длинных периодах (каждая 5-я и первая).
DATE_STEP_LONG = 5

#: Цвета оформления.
BACKGROUND = (255, 255, 255)
TEXT = (40, 40, 40)
MUTED = (120, 120, 120)
PULSE = (110, 110, 110)
AXIS = (60, 60, 60)
GRID = (200, 200, 200)

_RGBA_RE = re.compile(r"rgba\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)")


@dataclass(frozen=True)
class DayValues:
    """Средние за локальные сутки; None — в этот день нет значения."""

    day: date
    systolic: Optional[int] = None
    diastolic: Optional[int] = None
    pulse: Optional[int] = None


def zone_color(kind: str, value: int) -> tuple[int, int, int]:
    """Базовый RGB зоны значения (`kind` — `systolic` / `diastolic`), без альфы."""
    band = CHART_ZONES[kind][band_index(value, CHART_ZONES[kind])]
    match = _RGBA_RE.match(band["fill"])
    assert match is not None  # noqa: S101 — формат задан в chart_zones
    return tuple(int(match.group(i)) for i in (1, 2, 3))  # type: ignore[return-value]


def thresholds(kind: str) -> list[int]:
    """Пороги зон показателя (130, 140 / 85, 90) — для пунктирных линий."""
    return [band["to"] for band in CHART_ZONES[kind] if band["to"] is not None]


def daily_values(
    measurements: Iterable[Measurement], tz_name: str, start_day: date, end_day: date
) -> list[DayValues]:
    """Записи → средние по каждому дню периода (дни без записей — пустые).

    Числа — из `stats.daily_averages`: столбики и «Средние по дням» (§6.5)
    совпадают.
    """
    averages = stats_service.daily_averages(measurements, tz_name, start_day, end_day)
    return [
        DayValues(day=d.day, systolic=d.systolic, diastolic=d.diastolic, pulse=d.pulse)
        for d in averages.days
    ]


def scale_limits(days: Sequence[DayValues]) -> tuple[int, int]:
    """Верх шкалы САД и низ шкалы ДАД: не меньше 180 / 110, шаг 10."""
    top = max([SYS_MIN_TOP] + [d.systolic for d in days if d.systolic is not None])
    bottom = max([DIA_MIN_BOTTOM] + [d.diastolic for d in days if d.diastolic is not None])
    return (
        math.ceil(top / SCALE_STEP) * SCALE_STEP,
        math.ceil(bottom / SCALE_STEP) * SCALE_STEP,
    )


@dataclass(frozen=True)
class _Layout:
    """Геометрия области графика: средняя линия и пиксели на мм рт. ст."""

    plot_left: int
    plot_right: int
    plot_top: int
    plot_bottom: int
    mid: int
    sys_px: float
    dia_px: float

    def sys_y(self, value: int) -> float:
        """Y верха столбика САД (ниже базы — у линии)."""
        return self.mid - PULSE_BAND / 2 - max(0, value - SYS_BASE) * self.sys_px

    def dia_y(self, value: int) -> float:
        """Y низа столбика ДАД (ниже базы — у линии)."""
        return self.mid + PULSE_BAND / 2 + max(0, value - DIA_BASE) * self.dia_px


def _layout(top_value: int, bottom_value: int, label_room: int = 0) -> _Layout:
    """Геометрия: `label_room` — запас сверху и снизу под подписи значений."""
    plot_top, plot_bottom = TOP, HEIGHT - BOTTOM_AREA
    usable = plot_bottom - plot_top - PULSE_BAND - 2 * label_room
    sys_range, dia_range = top_value - SYS_BASE, bottom_value - DIA_BASE
    sys_h = usable * sys_range / (sys_range + dia_range)
    mid = round(plot_top + label_room + sys_h + PULSE_BAND / 2)
    return _Layout(
        plot_left=LEFT,
        plot_right=WIDTH - RIGHT,
        plot_top=plot_top,
        plot_bottom=plot_bottom,
        mid=mid,
        sys_px=sys_h / sys_range,
        dia_px=(usable - sys_h) / dia_range,
    )


def _font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONT_REGULAR_PATH), size)


def _text_center(draw: ImageDraw.ImageDraw, x: float, y: float, text: str, font, fill) -> None:
    """Текст с центром по x и верхом в y."""
    width = draw.textlength(text, font=font)
    draw.text((x - width / 2, y), text, font=font, fill=fill)


def _text_right(draw: ImageDraw.ImageDraw, right: float, y: float, text: str, font, fill) -> None:
    """Текст, выровненный по правому краю `right` (подписи шкалы)."""
    draw.text((right - draw.textlength(text, font=font), y), text, font=font, fill=fill)


def _dashed(draw: ImageDraw.ImageDraw, y: float, left: int, right: int) -> None:
    """Пунктирная горизонтальная линия порога."""
    x = left
    while x < right:
        draw.line([(x, y), (min(x + 8, right), y)], fill=GRID, width=1)
        x += 14


def _scale(
    draw: ImageDraw.ImageDraw,
    layout: _Layout,
    top_value: int,
    bottom_value: int,
    sizes: dict[str, int],
) -> None:
    """Шкала слева (подписи каждые 20 мм рт. ст.) и пунктир порогов зон."""
    font = _font(sizes["scale"])
    for value in thresholds("systolic"):
        _dashed(draw, layout.sys_y(value), layout.plot_left, layout.plot_right)
    for value in thresholds("diastolic"):
        _dashed(draw, layout.dia_y(value), layout.plot_left, layout.plot_right)
    for value in range(SYS_BASE + 10, top_value + 1, 20):
        y = layout.sys_y(value)
        _text_right(draw, layout.plot_left - 12, y - sizes["scale"] * 0.6, str(value), font, MUTED)
        draw.line([(layout.plot_left - 6, y), (layout.plot_left, y)], fill=AXIS)
    for value in range(DIA_BASE + 10, bottom_value + 1, 20):
        y = layout.dia_y(value)
        _text_right(draw, layout.plot_left - 12, y - sizes["scale"] * 0.6, str(value), font, MUTED)
        draw.line([(layout.plot_left - 6, y), (layout.plot_left, y)], fill=AXIS)
    draw.text((layout.plot_left + 10, 8), "мм рт. ст.", font=_font(FONT_UNIT), fill=MUTED)
    draw.line(
        [(layout.plot_left, layout.plot_top), (layout.plot_left, layout.plot_bottom)], fill=AXIS
    )
    for y in (layout.mid - PULSE_BAND / 2, layout.mid + PULSE_BAND / 2):
        draw.line([(layout.plot_left, y), (layout.plot_right, y)], fill=AXIS)


def _date_label(index: int, day: date, total: int) -> Optional[str]:
    """Подпись даты: первая — `ДД.ММ`, остальные — `ДД`; длинный период — каждая 5-я."""
    if index == 0:
        return f"{day:%d.%m}"
    if total > VALUE_LABELS_MAX_DAYS and index % DATE_STEP_LONG:
        return None
    return f"{day:%d}"


def _legend(draw: ImageDraw.ImageDraw, sizes: dict[str, int]) -> None:
    """Легенда снизу: цвета зон «САД / ДАД» и обозначение пульса (§6.5)."""
    font = _font(sizes["legend"])
    y = HEIGHT - 50
    x = LEFT
    sys_bands, dia_bands = CHART_ZONES["systolic"], CHART_ZONES["diastolic"]
    samples = (sys_bands[0]["to"] - 1, sys_bands[1]["to"] - 1, sys_bands[1]["to"])
    for index, sample in enumerate(samples):
        label = f"{sys_bands[index]['label']} / {dia_bands[index]['label']}"
        draw.rectangle([x, y + 4, x + 24, y + 28], fill=zone_color("systolic", sample))
        draw.text((x + 32, y), label, font=font, fill=TEXT)
        x += 32 + int(draw.textlength(label, font=font)) + 28
    draw.text((x, y), "72 — пульс", font=font, fill=PULSE)


def _labels_width(labels: Sequence[str], font: ImageFont.FreeTypeFont) -> float:
    """Максимальная ширина подписей, px (пусто — 0)."""
    probe = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    return max((probe.textlength(text, font=font) for text in labels), default=0.0)


def render(days: Sequence[DayValues], *, large: bool = False) -> bytes:
    """Нарисовать гистограмму (синхронно — через `asyncio.to_thread`, §7.2 п.7).

    `large` — крупный режим (UI11): кегли `FONT_SIZES["large"]`; если подпись
    значения шире столбика — `FONT_SIZES["large_tight"]` (переполнение).
    """
    top_value, bottom_value = scale_limits(days)
    show_values = len(days) <= VALUE_LABELS_MAX_DAYS
    layout = _layout(top_value, bottom_value, VALUE_LABEL_ROOM if show_values else 0)
    total = len(days)
    slot = (layout.plot_right - layout.plot_left) / max(total, 1)
    bar = max(4.0, slot * BAR_RATIO)
    sizes = FONT_SIZES["large" if large else "normal"]
    if large and show_values:
        labels = [str(v) for d in days for v in (d.systolic, d.diastolic) if v is not None]
        if _labels_width(labels, _font(sizes["value"])) > bar:
            sizes = FONT_SIZES["large_tight"]
    image = Image.new("RGB", (WIDTH, HEIGHT), BACKGROUND)
    draw = ImageDraw.Draw(image)
    _scale(draw, layout, top_value, bottom_value, sizes)

    value_font = _font(sizes["value"])
    pulse_font = _font(sizes["pulse"] if show_values else sizes["pulse_long"])
    date_font = _font(sizes["date"])
    line_top, line_bottom = layout.mid - PULSE_BAND / 2, layout.mid + PULSE_BAND / 2

    for index, day in enumerate(days):
        center = layout.plot_left + slot * (index + 0.5)
        left, right = center - bar / 2, center + bar / 2
        if day.systolic is not None:
            y = layout.sys_y(day.systolic)
            draw.rectangle(
                [left, min(y, line_top - 2), right, line_top],
                fill=zone_color("systolic", day.systolic),
            )
            if show_values:
                _text_center(draw, center, min(y, line_top - 2) - sizes["value"] - 6, str(day.systolic), value_font, TEXT)
        if day.diastolic is not None:
            y = layout.dia_y(day.diastolic)
            draw.rectangle(
                [left, line_bottom, right, max(y, line_bottom + 2)],
                fill=zone_color("diastolic", day.diastolic),
            )
            if show_values:
                _text_center(draw, center, max(y, line_bottom + 2) + 4, str(day.diastolic), value_font, TEXT)
        if day.pulse is not None:
            size = sizes["pulse"] if show_values else sizes["pulse_long"]
            _text_center(draw, center, layout.mid - size * 0.62, str(day.pulse), pulse_font, PULSE)
        label = _date_label(index, day.day, total)
        if label is not None:
            _text_center(draw, center, layout.plot_bottom + 12, label, date_font, MUTED)

    _legend(draw, sizes)
    buffer = io.BytesIO()
    image.save(buffer, "PNG", optimize=True)
    return buffer.getvalue()
