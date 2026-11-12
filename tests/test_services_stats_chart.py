"""Гистограмма статистики (bot/services/stats_chart.py, SPEC.md, §6.5).

Покрывается: PNG 1080 × 720, цвет половин столбика по своей зоне (пиксели
внутри столбика САД и ДАД), пустой день — белое место, значение ниже базы —
тонкая черта, расширение шкалы при значениях > 180 / > 110, средние по
локальным суткам и дни без записей, пульс есть / нет, подписи значений только
на коротком периоде, даты (каждая 5-я на длинном), кегли крупного режима (UI11:
словарь `FONT_SIZES` и fallback `large_tight` при переполнении подписи).
"""
from __future__ import annotations

import io
from datetime import date, datetime, timedelta, timezone

import pytest
from PIL import Image

from bot.models import Measurement
from bot.services import stats_chart
from bot.services.stats_chart import DayValues

D0 = date(2026, 11, 3)

GREEN = stats_chart.zone_color("systolic", 120)
YELLOW = stats_chart.zone_color("systolic", 135)
RED = stats_chart.zone_color("systolic", 150)


def week(*values) -> list[DayValues]:
    return [DayValues(D0 + timedelta(i), *v) for i, v in enumerate(values)]


def image_of(days) -> Image.Image:
    return Image.open(io.BytesIO(stats_chart.render(days))).convert("RGB")


def column_x(index: int, total: int) -> int:
    slot = (stats_chart.WIDTH - stats_chart.RIGHT - stats_chart.LEFT) / total
    return round(stats_chart.LEFT + slot * (index + 0.5))


def layout_for(days):
    top, bottom = stats_chart.scale_limits(days)
    room = stats_chart.VALUE_LABEL_ROOM if len(days) <= stats_chart.VALUE_LABELS_MAX_DAYS else 0
    return stats_chart._layout(top, bottom, room)


def pixel_above_mid(image, layout, x, offset=6) -> tuple:
    return image.getpixel((x, round(layout.mid - stats_chart.PULSE_BAND / 2 - offset)))


def pixel_below_mid(image, layout, x, offset=6) -> tuple:
    return image.getpixel((x, round(layout.mid + stats_chart.PULSE_BAND / 2 + offset)))


class TestColors:
    def test_zone_colors_are_opaque_base_rgb(self) -> None:
        assert GREEN == (30, 125, 79)
        assert YELLOW == (217, 164, 0)
        assert RED == (192, 57, 43)
        assert stats_chart.zone_color("diastolic", 84) == GREEN
        assert stats_chart.zone_color("diastolic", 85) == YELLOW
        assert stats_chart.zone_color("diastolic", 90) == RED

    def test_thresholds(self) -> None:
        assert stats_chart.thresholds("systolic") == [130, 140]
        assert stats_chart.thresholds("diastolic") == [85, 90]


class TestRender:
    def test_png_size(self) -> None:
        png = stats_chart.render(week((150, 94, 66)))
        image = Image.open(io.BytesIO(png))
        assert (image.format, image.size) == ("PNG", (1080, 720))

    def test_each_half_own_zone(self) -> None:
        """154/84: верх красный, низ зелёный; 135/88 — оба жёлтые."""
        days = week((154, 84, 66), (135, 88, 70), (120, 70, None))
        image, layout = image_of(days), layout_for(days)
        assert pixel_above_mid(image, layout, column_x(0, 3)) == RED
        assert pixel_below_mid(image, layout, column_x(0, 3)) == GREEN
        assert pixel_above_mid(image, layout, column_x(1, 3)) == YELLOW
        assert pixel_below_mid(image, layout, column_x(1, 3)) == YELLOW
        assert pixel_above_mid(image, layout, column_x(2, 3)) == GREEN

    def test_empty_day_is_blank(self) -> None:
        days = week((150, 95, 70), (None, None, None), (150, 95, 70))
        image, layout = image_of(days), layout_for(days)
        assert pixel_above_mid(image, layout, column_x(1, 3), 40) == stats_chart.BACKGROUND
        assert pixel_below_mid(image, layout, column_x(1, 3), 40) == stats_chart.BACKGROUND

    def test_height_grows_from_base(self) -> None:
        """САД 150 выше САД 120 — масштаб от базы 90, а не от 0."""
        days = week((150, 95, None), (120, 70, None))
        layout = layout_for(days)
        assert layout.sys_y(150) < layout.sys_y(120) < layout.sys_y(90)
        assert layout.dia_y(95) > layout.dia_y(70) > layout.dia_y(50)

    def test_below_base_is_thin_line(self) -> None:
        days = week((85, 45, 60))
        image, layout = image_of(days), layout_for(days)
        assert layout.sys_y(85) == layout.sys_y(90)
        x = column_x(0, 1)
        assert pixel_above_mid(image, layout, x, 1) == GREEN  # тонкая черта
        assert pixel_above_mid(image, layout, x, 20) == stats_chart.BACKGROUND

    def test_scale_expands(self) -> None:
        assert stats_chart.scale_limits(week((150, 95, None))) == (180, 110)
        assert stats_chart.scale_limits(week((183, 112, None))) == (190, 120)
        assert stats_chart.scale_limits(week((None, None, None))) == (180, 110)
        top_bar = layout_for(week((183, 112, None))).sys_y(183)
        assert top_bar >= stats_chart.TOP  # не вылезает за картинку

    def test_pulse_drawn_only_when_present(self) -> None:
        with_pulse, without = image_of(week((150, 95, 72))), image_of(week((150, 95, None)))
        layout = layout_for(week((150, 95, 72)))
        band = (
            column_x(0, 1) - 20,
            layout.mid - 10,
            column_x(0, 1) + 20,
            layout.mid + 10,
        )
        def colors(image) -> set:
            return {color for _, color in image.crop(band).getcolors(maxcolors=100_000)}

        plain = {stats_chart.BACKGROUND, stats_chart.AXIS}
        assert colors(with_pulse) - plain  # число пульса нарисовано
        assert colors(without) <= plain  # без пульса — пусто

    def test_value_labels_only_short_period(self, mocker) -> None:
        texts_drawn: list[str] = []
        original = stats_chart._text_center

        def spy(draw, x, y, text, font, fill):
            texts_drawn.append(text)
            return original(draw, x, y, text, font, fill)

        mocker.patch.object(stats_chart, "_text_center", side_effect=spy)
        stats_chart.render(week((151, 93, 66)))
        assert {"151", "93", "66"} <= set(texts_drawn)
        texts_drawn.clear()
        month = [DayValues(D0 + timedelta(i), 151, 93, 66) for i in range(30)]
        stats_chart.render(month)
        assert "151" not in texts_drawn and "93" not in texts_drawn
        assert texts_drawn.count("66") == 30  # пульс — у каждого дня

    @pytest.mark.parametrize(
        ("index", "total", "expected"),
        [(0, 7, "03.11"), (1, 7, "04"), (0, 30, "03.11"), (5, 30, "08"), (6, 30, None)],
    )
    def test_date_labels(self, index, total, expected) -> None:
        assert stats_chart._date_label(index, D0 + timedelta(index), total) == expected


class TestFontSizes:
    """Кегли крупного режима (UI11): словарь `FONT_SIZES` и переполнение подписи.

    `large` — кегли +30% с округлением вверх до чётного; если подпись значения
    шире столбика — `large_tight` (§13, ответы UI11).
    """

    def test_font_sizes_dictionary(self) -> None:
        """Те же числа, что задаёт UI11: 44/40/26/38/36 и fallback 42/38/24/36/34."""
        assert stats_chart.FONT_SIZES == {
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
        assert stats_chart.FONT_UNIT == 24  # единица измерения — вне пересчёта UI11

    def _rendered_sizes(self, mocker, days, *, large: bool) -> dict[str, int]:
        """Словарь кеглей, с которым реально отрендерен график (spy на `_legend`)."""
        seen: list[dict[str, int]] = []
        original = stats_chart._legend

        def spy(draw, sizes):
            seen.append(sizes)
            return original(draw, sizes)

        mocker.patch.object(stats_chart, "_legend", side_effect=spy)
        stats_chart.render(days, large=large)
        return seen[0]

    def test_large_fallback_when_label_wider_than_bar(self, mocker) -> None:
        """7 дней с САД=180: подпись при 44 px шире столбика — берётся `large_tight`."""
        days = week(*[(180, 110, 70)] * 7)
        top, bottom = stats_chart.scale_limits(days)
        layout = stats_chart._layout(top, bottom, stats_chart.VALUE_LABEL_ROOM)
        slot = (layout.plot_right - layout.plot_left) / len(days)
        bar = max(4.0, slot * stats_chart.BAR_RATIO)
        labels = [str(v) for d in days for v in (d.systolic, d.diastolic) if v is not None]
        width = stats_chart._labels_width(
            labels, stats_chart._font(stats_chart.FONT_SIZES["large"]["value"])
        )
        assert width > bar  # предусловие сценария: подпись шире столбика
        assert self._rendered_sizes(mocker, days, large=True) == stats_chart.FONT_SIZES[
            "large_tight"
        ]

    def test_large_keeps_sizes_when_labels_fit(self, mocker) -> None:
        """7 дней с САД=90: подпись уже столбика — остаётся `large` без ужима."""
        days = week(*[(90, 50, 60)] * 7)
        top, bottom = stats_chart.scale_limits(days)
        layout = stats_chart._layout(top, bottom, stats_chart.VALUE_LABEL_ROOM)
        slot = (layout.plot_right - layout.plot_left) / len(days)
        bar = max(4.0, slot * stats_chart.BAR_RATIO)
        labels = [str(v) for d in days for v in (d.systolic, d.diastolic) if v is not None]
        width = stats_chart._labels_width(
            labels, stats_chart._font(stats_chart.FONT_SIZES["large"]["value"])
        )
        assert width <= bar  # предусловие сценария: подпись влезает
        assert self._rendered_sizes(mocker, days, large=True) == stats_chart.FONT_SIZES["large"]

    def test_fallback_only_with_value_labels(self, mocker) -> None:
        """Переполнение проверяется только при подписях значений (≤ 7 дней)."""
        month = [DayValues(D0 + timedelta(i), 180, 110, 70) for i in range(30)]
        assert self._rendered_sizes(mocker, month, large=True) == stats_chart.FONT_SIZES["large"]

    def test_normal_mode_ignores_fallback(self, mocker) -> None:
        """Обычный режим не берёт `large_tight` даже при широких подписях."""
        days = week(*[(180, 110, 70)] * 7)
        assert self._rendered_sizes(mocker, days, large=False) == stats_chart.FONT_SIZES["normal"]


class TestDailyValues:
    def _m(self, moment: datetime, sys: int, dia: int, pulse=None) -> Measurement:
        return Measurement(user_id=1, timestamp=moment, systolic=sys, diastolic=dia, pulse=pulse)

    def test_averages_per_local_day(self) -> None:
        # 22:30 UTC 03.11 — это 01:30 04.11 по Минску
        rows = [
            self._m(datetime(2026, 11, 3, 6, 0, tzinfo=timezone.utc), 140, 90, 70),
            self._m(datetime(2026, 11, 3, 9, 0, tzinfo=timezone.utc), 131, 81, None),
            self._m(datetime(2026, 11, 3, 22, 30, tzinfo=timezone.utc), 120, 80, 60),
        ]
        days = stats_chart.daily_values(rows, "Europe/Minsk", D0, D0 + timedelta(days=2))
        assert [d.day for d in days] == [D0, D0 + timedelta(1), D0 + timedelta(2)]
        assert (days[0].systolic, days[0].diastolic, days[0].pulse) == (136, 86, 70)
        assert (days[1].systolic, days[1].pulse) == (120, 60)
        assert days[2] == DayValues(D0 + timedelta(2))  # пустой день

    def test_no_rows(self) -> None:
        days = stats_chart.daily_values([], "UTC", D0, D0 + timedelta(days=6))
        assert len(days) == 7 and all(d.systolic is None for d in days)


class TestCustomPeriodRender:
    """Свой период (§6.5, 0.5.3): от 1 до 31 столбика."""

    def test_single_day_with_values(self, mocker) -> None:
        drawn: list[str] = []
        original = stats_chart._text_center

        def spy(draw, x, y, text, font, fill):
            drawn.append(text)
            return original(draw, x, y, text, font, fill)

        mocker.patch.object(stats_chart, "_text_center", side_effect=spy)
        image = Image.open(io.BytesIO(stats_chart.render(week((150, 95, 70)))))
        assert image.size == (1080, 720)
        assert {"150", "95", "70", "03.11"} <= set(drawn)

    def test_31_days(self, mocker) -> None:
        drawn: list[str] = []
        original = stats_chart._text_center

        def spy(draw, x, y, text, font, fill):
            drawn.append(text)
            return original(draw, x, y, text, font, fill)

        mocker.patch.object(stats_chart, "_text_center", side_effect=spy)
        days = [DayValues(D0 + timedelta(i), 151, 93, 66) for i in range(31)]
        image = Image.open(io.BytesIO(stats_chart.render(days))).convert("RGB")
        assert image.size == (1080, 720)
        assert "151" not in drawn  # подписи значений — только при ≤ 7 днях
        assert drawn.count("66") == 31
        layout = layout_for(days)
        assert pixel_above_mid(image, layout, column_x(30, 31)) == RED

    @pytest.mark.parametrize(("total", "values"), [(8, False), (7, True), (2, True)])
    def test_value_labels_threshold(self, total, values) -> None:
        assert (total <= stats_chart.VALUE_LABELS_MAX_DAYS) is values
