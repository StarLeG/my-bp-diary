"""Рендер UI бота в обоих режимах (0.5.10, §11) — тексты и клавиатуры.

Крупный режим (`users.ui_large`) — только рендер: сводки по строке на поле
со значениями в `<b>`, дневник блоками, кнопки по одной в строке (пары
разъезжаются), калькулятор в 2 колонки, списки с пагинацией по 5
(«Показать ещё …»), шапки экранов — `<b>` всегда, пользовательский текст —
через `texts.esc` (UI5). Обычный режим — те же данные plain-текстом,
пары в ряд, калькулятор в 3 колонки.
"""
from __future__ import annotations

from datetime import date

import pytest
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from bot import keyboards, texts


def row_texts(markup: InlineKeyboardMarkup) -> list[list[str]]:
    """Подписи кнопок по рядам."""
    return [[button.text for button in row] for row in markup.inline_keyboard]


def row_data(markup: InlineKeyboardMarkup) -> list[list[str]]:
    """callback_data кнопок по рядам."""
    return [[button.callback_data or "" for button in row] for row in markup.inline_keyboard]


# ─────────────────── HTML-шапки (§11, UI5) ───────────────────


class TestScreenHeaders:
    def test_header_wraps_in_bold(self) -> None:
        assert texts.header("📊", "Статистика") == "<b>📊 Статистика</b>"

    @pytest.mark.parametrize("key", sorted(texts.SCREEN_HEADERS))
    def test_screen_header_bold(self, key: str) -> None:
        assert texts.screen_header(key) == f"<b>{texts.SCREEN_HEADERS[key]}</b>"
        assert texts.SCREEN_HEADERS[key], f"пустая шапка {key!r}"

    def test_screen_header_emoji_first(self) -> None:
        # Шапка — «[эмодзи] Заголовок»: эмодзи пробелом отделён от заголовка.
        for value in texts.SCREEN_HEADERS.values():
            emoji, title = value.split(" ", 1)
            assert title and not title.startswith(" ")

    def test_stats_days_header_carries_period(self) -> None:
        text = texts.STATS_DAYS_HEADER.format(date_from="01.11", date_to="07.11")
        assert text == f"{texts.screen_header('stats_days')} · 01.11–07.11"

    @pytest.mark.parametrize(
        "text",
        [texts.ARCHIVE_MENU, texts.MEDS_LIST_EMPTY, texts.ABOUT_TEXT, texts.SUPPORT_TEXT],
    )
    def test_screens_start_with_header(self, text: str) -> None:
        key = {
            texts.ARCHIVE_MENU: "archive",
            texts.MEDS_LIST_EMPTY: "meds",
            texts.ABOUT_TEXT: "about",
            texts.SUPPORT_TEXT: "support",
        }[text]
        assert text.startswith(texts.screen_header(key) + "\n\n")


# ─────────────── Сводки: по строке на поле, `<b>` в крупном (UI4) ───────────────


class TestSummariesBothModes:
    def test_saved_regular_one_line_plain(self) -> None:
        text = texts.fmt_saved(135, 85, 72)
        assert text == "✅ 135/85, пульс 72"
        assert "<b>" not in text

    def test_saved_large_bold_values(self) -> None:
        text = texts.fmt_saved(135, 85, 72, large=True)
        assert text == "✅ <b>135/85</b>, пульс <b>72</b>"

    def test_saved_without_pulse(self) -> None:
        assert texts.fmt_saved(135, 85) == "✅ 135/85"
        assert texts.fmt_saved(135, 85, large=True) == "✅ <b>135/85</b>"

    def test_updated_and_archive_saved_same_shape(self) -> None:
        updated = texts.fmt_updated(120, 70, None, large=True)
        assert updated == "✅ Обновлено: <b>120/70</b>"
        archive = texts.fmt_archive_saved("12.03.2025, 08:00", 135, 85, 72, large=True)
        assert archive == "✅ <b>135/85</b>, пульс <b>72</b> · за 12.03.2025, 08:00"

    def test_archive_saved_escapes_moment(self) -> None:
        text = texts.fmt_archive_saved("1 < 2", 135, 85)
        assert "1 &lt; 2" in text
        assert "1 < 2" not in text

    def test_stats_values_bold_only_in_large(self) -> None:
        args = (7, "01.11", "07.11", 5, 130, 120, 145, 82, 75, 90, 68,
                texts.STATS_TREND_STABLE, "130/80", texts.STATS_GOAL_OK)
        regular = texts.fmt_stats(*args)
        large = texts.fmt_stats(*args, large=True)
        assert "Систолическое: в среднем 130 (120…145)" in regular
        assert "<b>" not in regular
        assert "Систолическое: в среднем <b>130</b> (<b>120</b>…<b>145</b>)" in large
        assert large.splitlines()[1] == ""  # пустая строка после шапки

    def test_daily_averages_bold_values_in_large(self) -> None:
        rows = [("01.11", 0, 125, 80, 70)]
        total = (125, 80, 70)
        regular = texts.fmt_daily_averages("01.11", "01.11", rows, total)
        large = texts.fmt_daily_averages("01.11", "01.11", rows, total, large=True)
        assert "🟢 01.11 — 125/80, пульс 70" in regular
        assert "<b>125/80</b>" not in regular  # жирные значения — только в крупном
        assert "🟢 01.11 — <b>125/80</b>, пульс <b>70</b>" in large
        assert "Среднее за день: <b>125/80</b>" in large


# ─────────────────── Дневник блоками (мокап 2.3) ───────────────────


class TestDiaryBlocks:
    def test_regular_single_line(self) -> None:
        line = texts.fmt_diary_line("08:15", 135, 85, 72, True, "normal")
        assert line == "08:15 — 135/85, пульс 72 · лекарство: да · норма"

    def test_large_block_with_bold_values(self) -> None:
        line = texts.fmt_diary_line("08:15", 135, 85, 72, True, "normal", large=True)
        assert line == "08:15\n<b>135/85 · пульс 72</b>\nлекарство: да · норма"

    def test_large_block_without_statuses(self) -> None:
        line = texts.fmt_diary_line("08:15", 135, 85, 72, None, None, large=True)
        assert line == "08:15\n<b>135/85 · пульс 72</b>"

    def test_statuses_labels(self) -> None:
        line = texts.fmt_diary_line("21:40", 150, 95, None, False, "headache")
        assert line == "21:40 — 150/95 · лекарство: нет · головная боль"
        bold = texts.fmt_diary_line("21:40", 150, 95, None, False, "headache", large=True)
        assert bold.endswith("лекарство: нет · головная боль")
        assert "<b>150/95</b>" in bold  # без пульса — только давление


# ─────────────── Кнопки: по одной / пары (§11, UI3/UI6) ───────────────


class TestButtonsBothModes:
    @staticmethod
    def _pair_rows(markup: InlineKeyboardMarkup, first: str, second: str) -> tuple[list[str], list[str]]:
        """Ряды, в которых стоят первая и вторая кнопка пары."""
        rows = row_texts(markup)
        left = next(row for row in rows if first in row)
        right = next(row for row in rows if second in row)
        return left, right

    @pytest.mark.parametrize(
        "builder",
        [keyboards.medication_taken_kb, keyboards.archive_medication_taken_kb],
    )
    def test_yes_no_pair(self, builder) -> None:
        regular = builder()
        left, right = self._pair_rows(regular, texts.BTN_MED_YES, texts.BTN_MED_NO)
        assert left is right and len(left) == 2  # пара в одном ряду
        large = builder(large=True)
        left, right = self._pair_rows(large, texts.BTN_MED_YES, texts.BTN_MED_NO)
        assert left is not right and len(left) == len(right) == 1  # разъехались

    def test_done_reset_pair_on_keypad(self) -> None:
        regular = keyboards.keypad_kb("sys", (None, None, None))
        left, right = self._pair_rows(regular, texts.BTN_DONE, texts.BTN_RESET)
        assert left is right and len(left) == 2
        large = keyboards.keypad_kb("sys", (None, None, None), large=True)
        left, right = self._pair_rows(large, texts.BTN_DONE, texts.BTN_RESET)
        assert left is not right

    def test_sex_pair(self) -> None:
        regular = keyboards.profile_sex_kb()
        left, right = self._pair_rows(regular, texts.BTN_SEX_MALE, texts.BTN_SEX_FEMALE)
        assert left is right and len(left) == 2
        large = keyboards.profile_sex_kb(large=True)
        left, right = self._pair_rows(large, texts.BTN_SEX_MALE, texts.BTN_SEX_FEMALE)
        assert left is not right

    def test_invite_pair(self) -> None:
        regular = keyboards.doctor_invite_kb("code")
        left, right = self._pair_rows(regular, texts.BTN_ACCEPT, texts.BTN_DECLINE)
        assert left is right and len(left) == 2
        large = keyboards.doctor_invite_kb("code", large=True)
        left, right = self._pair_rows(large, texts.BTN_ACCEPT, texts.BTN_DECLINE)
        assert left is not right

    def test_period_pair(self) -> None:
        regular = keyboards.stats_period_kb()
        left, right = self._pair_rows(regular, texts.BTN_7_DAYS, texts.BTN_30_DAYS)
        assert left is right and len(left) == 2
        large = keyboards.stats_period_kb(large=True)
        left, right = self._pair_rows(large, texts.BTN_7_DAYS, texts.BTN_30_DAYS)
        assert left is not right

    def test_slot_message_pairs_and_skip_alone(self) -> None:
        day = date(2026, 11, 10)
        regular = keyboards.slot_message_kb("morning", day, True, "both")
        assert row_texts(regular) == [
            [texts.BTN_MEASURE, texts.BTN_TAKE_ALL],
            [texts.BTN_PARTIAL, texts.BTN_SNOOZE_30],
            [texts.BTN_SKIP_SLOT],
        ]
        large = keyboards.slot_message_kb("morning", day, True, "both", large=True)
        assert all(len(row) == 1 for row in large.inline_keyboard)
        assert len(large.inline_keyboard) == 5

    def test_slot_message_meds_mode_three_rows(self) -> None:
        """Режим «Только лекарства» — 3 строки (§11, мокап 2.4)."""
        day = date(2026, 11, 10)
        regular = keyboards.slot_message_kb("morning", day, True, "meds")
        assert row_texts(regular) == [
            [texts.BTN_TAKE_ALL, texts.BTN_PARTIAL],
            [texts.BTN_SNOOZE_30],
            [texts.BTN_SKIP_SLOT],
        ]
        assert len(regular.inline_keyboard) == 3

    def test_remeasure_pair_plus_alone(self) -> None:
        regular = keyboards.remeasure_kb(5)
        assert row_texts(regular) == [
            [texts.BTN_REMEASURE_NOW, texts.BTN_SNOOZE_30],
            [texts.BTN_REMEASURE_OFF],
        ]
        large = keyboards.remeasure_kb(5, large=True)
        assert row_texts(large) == [
            [texts.BTN_REMEASURE_NOW],
            [texts.BTN_SNOOZE_30],
            [texts.BTN_REMEASURE_OFF],
        ]

    def test_visit_notice_pairs(self) -> None:
        regular = keyboards.ap_notice_kb(7, "doctor", "eve", with_doctor=True)
        assert row_texts(regular) == [
            [texts.BTN_AP_SEEN, texts.BTN_AP_MOVE],
            [texts.BTN_AP_CANCEL["doctor"], texts.BTN_AP_REPORT],
            [texts.BTN_AP_SEND],
        ]
        large = keyboards.ap_notice_kb(7, "doctor", "eve", with_doctor=True, large=True)
        assert all(len(row) == 1 for row in large.inline_keyboard)
        assert len(large.inline_keyboard) == 5

    def test_feeling_grid_splits_in_large(self) -> None:
        regular = keyboards.feeling_kb()
        assert [len(row) for row in regular.inline_keyboard][:2] == [2, 2]  # сетка 2×2
        large = keyboards.feeling_kb(large=True)
        assert all(len(row) == 1 for row in large.inline_keyboard)


# ─────────── Калькулятор: 3 → 2 колонки (§6.3, мокап 2.1) ───────────


class TestKeypadColumns:
    def test_regular_calculator_columns(self) -> None:
        markup = keyboards.keypad_kb("sys", (None, None, None))
        assert row_data(markup)[:5] == [
            ["m:field:sys", "m:field:dia", "m:field:pulse"],
            ["m:key:7", "m:key:8", "m:key:9"],
            ["m:key:4", "m:key:5", "m:key:6"],
            ["m:key:1", "m:key:2", "m:key:3"],
            ["m:key:clear", "m:key:0", "m:key:bksp"],
        ]

    def test_large_two_columns(self) -> None:
        markup = keyboards.keypad_kb("sys", (None, None, None), large=True)
        rows = row_data(markup)
        assert rows[:3] == [["m:field:sys"], ["m:field:dia"], ["m:field:pulse"]]
        assert rows[3:8] == [
            ["m:key:1", "m:key:2"],
            ["m:key:3", "m:key:4"],
            ["m:key:5", "m:key:6"],
            ["m:key:7", "m:key:8"],
            ["m:key:9", "m:key:0"],
        ]
        assert rows[8:] == [
            ["m:key:clear", "m:key:bksp"],
            ["m:key:ok"],
            ["m:reset"],
            ["m:cancel"],
        ]

    def test_large_digits_never_single_column(self) -> None:
        """Исключение §11: калькулятор не схлопывается в один столбец."""
        markup = keyboards.keypad_kb("1", (None, None, None), large=True)
        for row in markup.inline_keyboard[3:8]:
            assert len(row) == 2


# ─────────────── Пагинация по 5: «Показать ещё …» (UI13) ───────────────


class TestPagination:
    def test_show_more_label_is_min(self) -> None:
        more = keyboards.show_more_button("x:10", 12, 5)
        assert more is not None
        assert more.text == "Показать ещё 5"  # min(5, 12 − 5)
        assert more.callback_data == "x:10"
        short = keyboards.show_more_button("x:10", 7, 5)
        assert short is not None and short.text == "Показать ещё 2"  # min(5, 2)
        single = keyboards.show_more_button("x:10", 6, 5)
        assert single is not None and single.text == "Показать ещё 1"

    def test_show_more_absent_when_all_shown(self) -> None:
        assert keyboards.show_more_button("x:10", 10, 10) is None
        assert keyboards.show_more_button("x:10", 3, 5) is None

    def test_stats_days_pagination(self) -> None:
        markup = keyboards.stats_days_kb("7", large=True, total=12, limit=5)
        rows = row_texts(markup)
        assert rows[0] == ["Показать ещё 5"]
        assert row_data(markup)[0] == ["stats:days:7:10"]
        assert rows[-1] == [texts.BTN_BACK]
        regular = keyboards.stats_days_kb("7", total=12, limit=5)
        assert row_texts(regular) == [[texts.BTN_BACK]]  # обычный — весь список

    def test_doctor_patients_pagination(self) -> None:
        patients = [(index, f"Пациент {index}") for index in range(1, 13)]
        markup = keyboards.doctor_patients_kb(patients, large=True, limit=5)
        rows = row_texts(markup)
        assert len(rows) == 7  # 5 пациентов + «Показать ещё» + «Назад»
        assert rows[5] == ["Показать ещё 5"]
        assert row_data(markup)[5] == ["doc:patients:10"]
        full = keyboards.doctor_patients_kb(patients)
        assert len(full.inline_keyboard) == 13  # все 12 + «Назад», без пагинации

    def test_appointment_lists_pagination(self) -> None:
        items = [(index, f"Визит {index}") for index in range(1, 13)]
        listing = keyboards.ap_list_kb(items, has_past=True, large=True, limit=5)
        assert row_data(listing)[5] == ["ap:list:10"]
        assert row_texts(listing)[5] == ["Показать ещё 5"]
        past = keyboards.ap_past_kb(items[:5] * 4, large=True, limit=15)
        assert row_data(past)[15] == ["ap:past:20"]
        assert row_texts(past)[15] == ["Показать ещё 5"]
        full = keyboards.ap_past_kb(items[:5] * 4, large=True, limit=20)
        assert "Показать ещё" not in [t for row in row_texts(full) for t in row]

    def test_no_pagination_in_regular_mode(self) -> None:
        items = [(index, f"Визит {index}") for index in range(1, 13)]
        for markup in (
            keyboards.ap_list_kb(items, has_past=False, limit=5),
            keyboards.ap_past_kb(items, limit=5),
            keyboards.doctor_patients_kb(items, limit=5),
        ):
            texts_flat = [text for row in row_texts(markup) for text in row]
            assert not any(text.startswith("Показать ещё") for text in texts_flat)


# ─────────── Экранирование пользовательского текста (§11, UI5) ───────────


class TestUserTextEscaping:
    DANGER = "Лозартан <бета> & \"фирма\""

    def test_esc_helper(self) -> None:
        # `html.escape` с кавычками по умолчанию — как в `bot/texts.py`.
        assert texts.esc("a<b>&'") == "a&lt;b&gt;&amp;&#x27;"
        assert texts.esc(135) == "135"  # объекты приводятся к строке

    def test_meds_intake_escaped_both_modes(self) -> None:
        regular = texts.fmt_meds_intake([self.DANGER])
        assert "Лозартан &lt;бета&gt;" in regular
        assert "<b>" not in regular
        large = texts.fmt_meds_intake([self.DANGER], large=True)
        assert "<b>Лозартан &lt;бета&gt;" in large

    def test_intake_result_escaped(self) -> None:
        text = texts.fmt_intake_result([], [], [self.DANGER])
        assert "&lt;бета&gt;" in text
        mixed = texts.fmt_intake_result([self.DANGER], ["Амлодипин"], [])
        assert "&lt;бета&gt;" in mixed and "Амлодипин" in mixed

    def test_slot_message_escapes_medication_names(self) -> None:
        regular = texts.fmt_slot_message("morning", [self.DANGER], "both")
        assert "💊 Лекарства: Лозартан &lt;бета&gt;" in regular
        assert "<b>" not in regular
        large = texts.fmt_slot_message("morning", [self.DANGER], "both", large=True)
        assert "<b>Лозартан &lt;бета&gt;" in large

    def test_meds_summary_escaped(self) -> None:
        text = texts.fmt_meds_summary([self.DANGER], ["Амлодипин 5 мг"])
        assert "принято: Лозартан &lt;бета&gt;" in text
        assert "пропущено: Амлодипин 5 мг" in text

    def test_onboarding_confirmations_escape(self) -> None:
        tz = texts.fmt_onb_tz_confirm("Europe/<Minsk>", "+03:00")
        assert "Europe/&lt;Minsk&gt;" in tz
        target = texts.fmt_onb_target_confirm("13<0", large=True)
        assert "<b>13&lt;0</b>" in target
